"""Business entry for registering deliveries, plus the delivery read model.

Businesses register a delivery inside their own authorized transaction, so a message exists
exactly when the business change commits. Content and timing are authorized by the business
entry; recipients are limited to configured destinations, users bound on an app channel and
the conversations of the business's own commands.
"""

import json
from datetime import datetime

from sqlalchemy import select

from keystone.core.errors import ServiceError
from keystone.core.time import utcnow
from keystone.platform.identity.services.subjects import users_in_transaction
from keystone.platform.messaging.adapters import feishu_errors
from keystone.platform.messaging.models import (
    ExternalAccount,
    MessageChannel,
    MessageDelivery,
    MessageDestination,
    MessageInbound,
)
from keystone.platform.messaging.schemas.content import MAX_CONTENT_BYTES, content_adapter

SOURCE_FIELDS = {"source_module": 48, "source_type": 64, "source_id": 64}


def delivery_view(row, destination_key=None, recipient=None):
    # Content stays with the business; this view only states facts about the delivery.
    return {
        "id": row.id,
        "channel_id": row.channel_id,
        "destination_key": destination_key,
        "recipient": recipient,
        "reply_inbound_id": row.reply_inbound_id,
        "source_module": row.source_module,
        "source_type": row.source_type,
        "source_id": row.source_id,
        "kind": row.kind,
        "content_mode": row.content_mode,
        "status": row.status,
        "attempts": row.attempts,
        "error_code": row.error_code,
        "error_hint": feishu_errors.hint(row.error_code),
        "expires_at": row.expires_at,
        "created_at": row.created_at,
        "sent_at": row.sent_at,
    }


async def user_is_active(session, user_id):
    found = await users_in_transaction(session, [user_id])
    return bool(found) and found[0]["is_active"]


def checked_content(content):
    """Validate neutral content; raises ServiceError when it is too large to keep or send."""
    content = content_adapter.validate_python(content).model_dump(mode="json")
    if len(json.dumps(content, ensure_ascii=False).encode()) > MAX_CONTENT_BYTES:
        raise ServiceError(422, "message_content_too_large", "消息内容过长")
    return content


async def register(
    session, created_by, channel_id, content, source, expires_at, *, supplied=None, **target
):
    """Insert a pending delivery to a resolved channel and target; checks are the caller's.

    With `supplied` ("text" or "card") no content is stored: the source's content provider is
    asked for it right before each send.
    """
    if (content is None) == (supplied is None) or supplied not in (None, "text", "card"):
        raise ValueError("Pass either content or the kind of supplied content")
    if content is not None:
        content = checked_content(content)
    if any(
        not isinstance(value, str) or not value or len(value) > SOURCE_FIELDS[name]
        for name, value in source.items()
    ):
        raise ValueError("Invalid delivery source")
    if expires_at is not None and expires_at <= utcnow():
        raise ServiceError(422, "message_already_expired", "投递截止时间已过")
    row = MessageDelivery(
        channel_id=channel_id,
        kind=supplied or content["kind"],
        content_mode="supplied" if supplied else "stored",
        content=content,
        expires_at=expires_at,
        status="pending",
        attempts=0,
        created_by=created_by,
        **source,
        **target,
    )
    session.add(row)
    await session.flush()
    return row.id


async def user_channel(session, user_id):
    """The active app channel a user is reached on: their earliest binding wins."""
    return await session.scalar(
        select(ExternalAccount.channel_id)
        .join(MessageChannel, ExternalAccount.channel_id == MessageChannel.id)
        .where(ExternalAccount.user_id == user_id, MessageChannel.is_active.is_(True))
        .order_by(ExternalAccount.created_at, ExternalAccount.id)
        .limit(1)
    )


async def enqueue_in_transaction(
    session,
    auth,
    *,
    source_module,
    source_type,
    source_id,
    content=None,
    supplied=None,
    destination_key=None,
    user_id=None,
    inbound_id=None,
    expires_at: datetime | None = None,
):
    """Register a pending delivery in the caller's transaction and return its ID.

    Pass exactly one target: a configured destination key, a Keystone user bound on an
    active app channel, or the inbound ID of one of this module's commands to reply to it.
    Pass either content or `supplied="text"|"card"` to fetch the content when sending.
    """
    if sum(value is not None for value in (destination_key, user_id, inbound_id)) != 1:
        raise ValueError("Pass exactly one of destination_key, user_id or inbound_id")
    source = {"source_module": source_module, "source_type": source_type, "source_id": source_id}
    created_by = auth.user.id
    options = {"supplied": supplied}
    if inbound_id is not None:
        inbound = await session.get(MessageInbound, inbound_id)
        # Only the module whose command was received answers in that conversation.
        if inbound is None or inbound.kind != "command" or inbound.command_module != source_module:
            raise ServiceError(404, "message_reply_unavailable", "要回复的命令不存在")
        channel = await session.get(MessageChannel, inbound.channel_id)
        if not channel.is_active:
            raise ServiceError(409, "message_destination_unavailable", "消息渠道已停用")
        return await register(
            session,
            created_by,
            channel.id,
            content,
            source,
            expires_at,
            reply_inbound_id=inbound.id,
            **options,
        )
    if user_id is not None:
        channel_id = await user_channel(session, user_id)
        if channel_id is None:
            raise ServiceError(409, "message_recipient_unbound", "该用户没有绑定可用的飞书账号")
        if not await user_is_active(session, user_id):
            raise ServiceError(409, "message_recipient_disabled", "该用户已停用")
        return await register(
            session,
            created_by,
            channel_id,
            content,
            source,
            expires_at,
            recipient_user_id=user_id,
            **options,
        )
    destination = await session.scalar(
        select(MessageDestination).where(MessageDestination.key == destination_key)
    )
    channel = destination and await session.get(MessageChannel, destination.channel_id)
    if destination is None or not destination.is_active or not channel.is_active:
        raise ServiceError(409, "message_destination_unavailable", "消息接收方不存在或已停用")
    if content and content.get("actions") and channel.provider != "feishu_app":
        # Group bots cannot receive callbacks, so their buttons could never be answered.
        raise ServiceError(422, "message_actions_unsupported", "群机器人不支持带动作的卡片")
    return await register(
        session,
        created_by,
        channel.id,
        content,
        source,
        expires_at,
        destination_id=destination.id,
        **options,
    )


def filters(model, channel_id, status):
    """Optional list filters shared by the delivery and inbound records."""
    conditions = []
    if channel_id is not None:
        conditions.append(model.channel_id == channel_id)
    if status is not None:
        conditions.append(model.status == status)
    return conditions


class DeliveryService:
    def __init__(self, identity):
        self.identity = identity

    async def listing(self, offset, limit, channel_id=None, status=None):
        async with self.identity.access("message_delivery:read") as (session, _):
            rows = (
                await session.execute(
                    select(MessageDelivery, MessageDestination.key)
                    .outerjoin(
                        MessageDestination, MessageDelivery.destination_id == MessageDestination.id
                    )
                    .where(*filters(MessageDelivery, channel_id, status))
                    .order_by(MessageDelivery.created_at.desc(), MessageDelivery.id.desc())
                    .offset(offset)
                    .limit(limit)
                )
            ).all()
            users = {
                row["id"]: row["label"]
                for row in await users_in_transaction(
                    session, {row.recipient_user_id for row, _ in rows if row.recipient_user_id}
                )
            }
            return [
                delivery_view(
                    row,
                    key,
                    row.recipient_user_id
                    and {"id": row.recipient_user_id, "label": users.get(row.recipient_user_id)},
                )
                for row, key in rows
            ]

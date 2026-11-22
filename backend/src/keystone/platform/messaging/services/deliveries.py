"""Business entry for registering deliveries, plus the delivery read model.

Businesses register a delivery inside their own authorized transaction, so a message exists
exactly when the business change commits. Content and timing are authorized by the business
entry; recipients are limited to configured destinations.
"""

import json
from datetime import datetime

from sqlalchemy import select

from keystone.core.errors import ServiceError
from keystone.core.time import utcnow
from keystone.platform.messaging.models import MessageChannel, MessageDelivery, MessageDestination
from keystone.platform.messaging.schemas.content import MAX_CONTENT_BYTES, content_adapter

SOURCE_FIELDS = {"source_module": 48, "source_type": 64, "source_id": 64}


def delivery_view(row, destination_key=None):
    # Content stays with the business; this view only states facts about the delivery.
    return {
        "id": row.id,
        "channel_id": row.channel_id,
        "destination_key": destination_key,
        "source_module": row.source_module,
        "source_type": row.source_type,
        "source_id": row.source_id,
        "kind": row.kind,
        "status": row.status,
        "attempts": row.attempts,
        "error_code": row.error_code,
        "expires_at": row.expires_at,
        "created_at": row.created_at,
        "sent_at": row.sent_at,
    }


async def enqueue_in_transaction(
    session,
    auth,
    *,
    destination_key,
    content,
    source_module,
    source_type,
    source_id,
    expires_at: datetime | None = None,
):
    """Register a pending delivery in the caller's transaction and return its ID."""
    content = content_adapter.validate_python(content).model_dump(mode="json")
    if len(json.dumps(content, ensure_ascii=False).encode()) > MAX_CONTENT_BYTES:
        raise ServiceError(422, "message_content_too_large", "消息内容过长")
    source = {"source_module": source_module, "source_type": source_type, "source_id": source_id}
    if any(
        not isinstance(value, str) or not value or len(value) > SOURCE_FIELDS[name]
        for name, value in source.items()
    ):
        raise ValueError("Invalid delivery source")
    if expires_at is not None and expires_at <= utcnow():
        raise ServiceError(422, "message_already_expired", "投递截止时间已过")
    destination = await session.scalar(
        select(MessageDestination).where(MessageDestination.key == destination_key)
    )
    channel = destination and await session.get(MessageChannel, destination.channel_id)
    if destination is None or not destination.is_active or not channel.is_active:
        raise ServiceError(409, "message_destination_unavailable", "消息接收方不存在或已停用")
    row = MessageDelivery(
        channel_id=channel.id,
        destination_id=destination.id,
        kind=content["kind"],
        content=content,
        expires_at=expires_at,
        status="pending",
        attempts=0,
        created_by=auth.user.id,
        **source,
    )
    session.add(row)
    await session.flush()
    return row.id


class DeliveryService:
    def __init__(self, identity):
        self.identity = identity

    async def listing(self, offset, limit):
        async with self.identity.access("message_delivery:read") as (session, _):
            rows = await session.execute(
                select(MessageDelivery, MessageDestination.key)
                .join(MessageDestination, MessageDelivery.destination_id == MessageDestination.id)
                .order_by(MessageDelivery.created_at.desc(), MessageDelivery.id.desc())
                .offset(offset)
                .limit(limit)
            )
            return [delivery_view(row, key) for row, key in rows]

"""Inbound events: card actions run as the clicker's bound user, binding codes bind a user.

Events arrive on a Worker's long connection and are answered within Feishu's 3 seconds.
Each is stored once by its de-duplication key, then processed right away; one that does not
finish in time stays `received` and the Worker's periodic pass finishes it. Bodies, binding
codes and message text are never stored or logged.
"""

import asyncio
import contextlib
import hashlib
import json
import logging
import re
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from keystone.core.errors import ServiceError
from keystone.core.time import utcnow
from keystone.platform.audit.services import writer as audit
from keystone.platform.audit.services.writer import Actor
from keystone.platform.identity.services.access import AccessContext
from keystone.platform.identity.services.authorization import load_authorization
from keystone.platform.identity.services.subjects import users_in_transaction
from keystone.platform.messaging.adapters import feishu_app, feishu_content, feishu_errors
from keystone.platform.messaging.models import (
    ExternalAccount,
    MessageBindCode,
    MessageChannel,
    MessageDelivery,
    MessageInbound,
)

log = logging.getLogger(__name__)

# Inline processing stops here, inside Feishu's 3 seconds; the periodic pass finishes the rest.
BUDGET_SECONDS = 2.0
# Events processed at once in one Worker. Beyond this a click is stored and answered at once,
# and the periodic pass processes it, so a burst cannot crowd Worker tasks out of the
# database pool or the event loop.
INLINE_LIMIT = 8
LEASE = timedelta(seconds=60)
MAX_ATTEMPTS = 3
BATCH = 20
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LIFETIME = timedelta(minutes=10)
BIND_MESSAGE = re.compile(r"^\s*绑定\s*([A-Za-z0-9]{8})\s*$")
# Wrong codes per Feishu account per hour before binding is refused for a while.
BIND_FAILURES = 5
BIND_REPLIES = {
    "bind_code_invalid": "绑定码无效或已过期，请在 Keystone 个人设置中重新生成。",
    "bind_account_taken": "这个飞书账号已绑定其他 Keystone 用户，请联系管理员先解除绑定。",
    "bind_user_disabled": "生成绑定码的 Keystone 用户已停用。",
    "bind_rate_limited": "尝试次数过多，请一小时后再试。",
}


def digest(code):
    return hashlib.sha256(code.upper().encode()).hexdigest()


def inbound_view(row, actor=None):
    return {
        "id": row.id,
        "channel_id": row.channel_id,
        "kind": row.kind,
        "delivery_id": row.delivery_id,
        "action_key": row.action_key,
        "external_user_id": row.external_user_id,
        "actor": actor,
        "status": row.status,
        "attempts": row.attempts,
        "error_code": row.error_code,
        "error_hint": feishu_errors.hint(row.error_code),
        "received_at": row.received_at,
        "processed_at": row.processed_at,
    }


class ActingUser(AccessContext):
    """The bound user's current role permissions; no PAT scope, nothing from the sender."""

    def __init__(self, runtime, user_id, inbound_id):
        super().__init__(runtime.database, runtime.settings, correlation_id=str(inbound_id))
        self.user_id = user_id

    async def authorize(self, session):
        return await load_authorization(session, self.user_id)


class Refused(Exception):
    def __init__(self, code, message, actor_id=None):
        self.code, self.message, self.actor_id = code, message, actor_id


def _occurred(header):
    value = header.get("create_time")
    try:
        return datetime.fromtimestamp(int(value) / 1000, UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _short(value):
    return value if isinstance(value, str) and 0 < len(value) <= 64 else None


async def _store(database, channel_id, **values):
    """Insert once per de-duplication key; return (id, status, is_new)."""
    async with database.transaction() as session:
        inserted = await session.scalar(
            insert(MessageInbound)
            .values(channel_id=channel_id, status="received", attempts=0, **values)
            .on_conflict_do_nothing(constraint="uq_message_inbound_dedupe")
            .returning(MessageInbound.id)
        )
        if inserted is not None:
            return inserted, "received", True
        row = (
            await session.execute(
                select(MessageInbound.id, MessageInbound.status).where(
                    MessageInbound.channel_id == channel_id,
                    MessageInbound.dedupe_key == values["dedupe_key"],
                )
            )
        ).one()
        return row.id, row.status, False


# --- card actions


async def _claim(database, inbound_id):
    """Take the processing lease; None while another processor holds it."""
    now = utcnow()
    async with database.transaction() as session:
        row = await session.get(MessageInbound, inbound_id, with_for_update=True)
        if row is None or row.status != "received":
            return None
        if row.attempts and row.updated_at > now - LEASE:
            return None
        if row.attempts >= MAX_ATTEMPTS:
            row.status, row.error_code, row.updated_at = "failed", "inbound_unfinished", now
            return None
        row.attempts, row.updated_at = row.attempts + 1, now
        return {
            "id": row.id,
            "channel_id": row.channel_id,
            "delivery_id": row.delivery_id,
            "action_key": row.action_key,
            "provider_message_id": row.provider_message_id,
            "external_user_id": row.external_user_id,
        }


async def _resolve(runtime, item):
    """Check the click against its delivery and bind it to the clicker's Keystone user."""
    async with runtime.database.transaction() as session:
        channel = await session.get(MessageChannel, item["channel_id"])
        delivery = item["delivery_id"] and await session.get(MessageDelivery, item["delivery_id"])
        actions = {
            action["key"]: action
            for action in ((delivery and delivery.content.get("actions")) or [])
        }
        # Only the delivery ID and action key come from the card; both must match the
        # message that was clicked, so a value copied into another card is refused.
        if (
            delivery is None
            or delivery.channel_id != item["channel_id"]
            or delivery.provider_message_id != item["provider_message_id"]
            or item["action_key"] not in actions
        ):
            raise Refused("message_action_mismatch", "这张卡片的操作已失效")
        if not channel.is_active:
            raise Refused("message_channel_disabled", "该应用已停用")
        declared = runtime.message_actions.get(
            (delivery.source_module, delivery.source_type, item["action_key"])
        )
        if declared is None:
            raise Refused("message_action_unknown", "这项操作已不再支持")
        user_id = await session.scalar(
            select(ExternalAccount.user_id).where(
                ExternalAccount.channel_id == item["channel_id"],
                ExternalAccount.external_user_id == item["external_user_id"],
            )
        )
        if user_id is None:
            raise Refused(
                "message_actor_unbound",
                "你的飞书账号还没有绑定 Keystone 用户，请先私聊应用发送绑定码",
            )
        found = await users_in_transaction(session, [user_id])
        if not found or not found[0]["is_active"]:
            raise Refused("message_actor_disabled", "你绑定的 Keystone 用户已停用", user_id)
        return declared, delivery, actions[item["action_key"]], found[0]


async def _finish(database, inbound_id, status, error_code=None, actor_id=None):
    now = utcnow()
    async with database.transaction() as session:
        await session.execute(
            update(MessageInbound)
            .where(MessageInbound.id == inbound_id, MessageInbound.status == "received")
            .values(
                status=status,
                error_code=error_code,
                actor_id=actor_id,
                updated_at=now,
                processed_at=now if status == "processed" else None,
            )
        )


async def process(runtime, inbound_id):
    """Process one claimed card action; returns the outcome, or None if not claimed."""
    item = await _claim(runtime.database, inbound_id)
    if item is None:
        return None
    try:
        declared, delivery, action, user = await _resolve(runtime, item)
        identity = ActingUser(runtime, user["id"], inbound_id)
        if declared.permission:
            try:
                async with identity.access(declared.permission):
                    pass
            except ServiceError as error:
                raise Refused(error.code, "你没有执行这项操作的权限", user["id"]) from None
        try:
            await declared.handler(runtime, identity, delivery.source_id, action["key"], inbound_id)
        except ServiceError as error:
            # A business refusal is the business's to audit; only the outcome is kept here.
            await _finish(runtime.database, inbound_id, "rejected", error.code, user["id"])
            return {"status": "rejected", "message": error.detail}
    except Refused as refused:
        await _finish(runtime.database, inbound_id, "rejected", refused.code, refused.actor_id)
        await audit.failed(
            runtime.database,
            "messaging.inbound.card_action",
            Actor("user", refused.actor_id) if refused.actor_id else Actor("system"),
            "messaging.inbound",
            inbound_id,
            str(inbound_id),
            refused.code,
            True,
        )
        return {"status": "rejected", "message": refused.message}
    await _finish(runtime.database, inbound_id, "processed", actor_id=user["id"])
    card = feishu_content.action_card(
        delivery.content, delivery.id, outcome=f"{user['label']} 已{action['text']}"
    )
    return {"status": "processed", "card": card, "done": f"已{action['text']}"}


def _card_answer(outcome):
    if outcome is None:
        return {"toast": {"type": "info", "content": "已收到，正在处理"}}
    if outcome["status"] == "processed":
        return {
            "toast": {"type": "success", "content": outcome["done"]},
            "card": {"type": "raw", "data": outcome["card"]},
        }
    return {"toast": {"type": "error", "content": outcome["message"]}}


async def receive_card_action(runtime, channel_id, header, event, slots=None):
    value = (event.get("action") or {}).get("value")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = None
    value = value if isinstance(value, dict) else {}
    try:
        delivery_id = UUID(str(value.get("delivery_id")))
    except ValueError:
        delivery_id = None
    event_id = _short(header.get("event_id"))
    open_id = _short((event.get("operator") or {}).get("open_id"))
    if event_id is None or open_id is None or delivery_id is None:
        return {"toast": {"type": "error", "content": "无法识别这项操作"}}
    inbound_id, status, new = await _store(
        runtime.database,
        channel_id,
        dedupe_key=event_id,
        kind="card_action",
        delivery_id=delivery_id,
        action_key=_short(value.get("action")),
        provider_message_id=_short((event.get("context") or {}).get("open_message_id")),
        external_user_id=open_id,
        occurred_at=_occurred(header),
    )
    if not new:
        if status == "processed":
            return {"toast": {"type": "success", "content": "已处理"}}
        return _card_answer(None)
    if slots is not None and slots.locked():
        return _card_answer(None)
    try:
        async with slots or contextlib.nullcontext(), asyncio.timeout(BUDGET_SECONDS):
            outcome = await process(runtime, inbound_id)
    except TimeoutError:
        # Cancelled mid-way: the business transaction rolled back, the lease expires and the
        # periodic pass processes it again.
        outcome = None
    return _card_answer(outcome)


async def retry(runtime, vault):
    """Finish card actions that inline processing did not; returns how many were tried."""
    from keystone.platform.messaging.services.channels import unseal

    async with runtime.database.transaction() as session:
        rows = (
            await session.execute(
                select(
                    MessageInbound.id, MessageInbound.channel_id, MessageInbound.provider_message_id
                )
                .where(
                    MessageInbound.status == "received",
                    # Never claimed (deferred under load), or a claim whose lease ran out.
                    or_(
                        MessageInbound.attempts == 0,
                        MessageInbound.updated_at < utcnow() - LEASE,
                    ),
                )
                .order_by(MessageInbound.updated_at, MessageInbound.id)
                .limit(BATCH)
            )
        ).all()
    for inbound_id, channel_id, message_id in rows:
        outcome = await process(runtime, inbound_id)
        if not outcome or outcome["status"] != "processed" or not message_id:
            continue
        # Nobody is waiting for an answer any more, so the card is updated in place.
        try:
            async with runtime.database.transaction() as session:
                channel = await session.get(MessageChannel, channel_id)
            secret = unseal(vault, channel)["app_secret"]
            await feishu_app.update_card(channel.app_id, secret, message_id, outcome["card"])
        except (ServiceError, feishu_app.Rejected, feishu_app.NotSent, feishu_app.Unknown):
            log.warning("messaging_card_update_failed inbound=%s", inbound_id)
    return len(rows)


# --- self-binding


async def _bind(runtime, channel_id, message_id, open_id, header, code):
    """Store, check and apply a binding message in one transaction; returns the reply code."""
    now = utcnow()
    async with runtime.database.transaction() as session:
        inbound_id = await session.scalar(
            insert(MessageInbound)
            .values(
                channel_id=channel_id,
                dedupe_key=message_id,
                kind="bind",
                external_user_id=open_id,
                occurred_at=_occurred(header),
                status="received",
                attempts=1,
            )
            .on_conflict_do_nothing(constraint="uq_message_inbound_dedupe")
            .returning(MessageInbound.id)
        )
        if inbound_id is None:
            return None, None
        row = await session.get(MessageInbound, inbound_id)
        failures = await session.scalar(
            select(func.count())
            .select_from(MessageInbound)
            .where(
                MessageInbound.channel_id == channel_id,
                MessageInbound.external_user_id == open_id,
                MessageInbound.kind == "bind",
                MessageInbound.status == "rejected",
                MessageInbound.received_at > now - timedelta(hours=1),
            )
        )
        result, user = None, None
        if failures >= BIND_FAILURES:
            result = "bind_rate_limited"
        else:
            bind_code = await session.scalar(
                select(MessageBindCode)
                .where(MessageBindCode.code_digest == digest(code))
                .with_for_update()
            )
            if bind_code is None or bind_code.expires_at <= now:
                result = "bind_code_invalid"
            else:
                found = await users_in_transaction(session, [bind_code.user_id])
                user = found[0] if found else None
                taken = await session.scalar(
                    select(ExternalAccount.user_id).where(
                        ExternalAccount.channel_id == channel_id,
                        ExternalAccount.external_user_id == open_id,
                    )
                )
                if user is None or not user["is_active"]:
                    result = "bind_user_disabled"
                elif taken is not None and taken != user["id"]:
                    result = "bind_account_taken"
                else:
                    await session.delete(bind_code)
                    if taken is None:
                        await _apply_binding(session, channel_id, user["id"], open_id)
        row.status = "rejected" if result else "processed"
        row.error_code, row.actor_id = result, user and user["id"]
        row.updated_at, row.processed_at = now, None if result else now
        return result, user


async def _apply_binding(session, channel_id, user_id, open_id):
    account = await session.scalar(
        select(ExternalAccount)
        .where(ExternalAccount.channel_id == channel_id, ExternalAccount.user_id == user_id)
        .with_for_update()
    )
    replaced = account is not None
    if account is None:
        account = ExternalAccount(
            channel_id=channel_id, user_id=user_id, external_user_id=open_id, created_by=user_id
        )
        session.add(account)
    else:
        # Rebinding moves the user to the Feishu account that proved it holds the code.
        account.external_user_id = open_id
    await session.flush()
    await audit.record(
        session,
        "messaging.account.self_bind",
        Actor("user", user_id),
        "messaging.account",
        account.id,
        details={
            "channel_id": str(channel_id),
            "external_user_id": open_id,
            "replaced": replaced,
            "via": "messaging",
        },
    )


async def receive_message(runtime, channel, header, event, slots=None):
    """Only private binding messages are handled; everything else is ignored unstored."""
    message = event.get("message") or {}
    sender = event.get("sender") or {}
    if (
        message.get("chat_type") != "p2p"
        or message.get("message_type") != "text"
        or sender.get("sender_type") != "user"
    ):
        return None
    try:
        text = json.loads(message.get("content") or "").get("text")
    except (ValueError, AttributeError):
        return None
    matched = isinstance(text, str) and BIND_MESSAGE.fullmatch(text)
    message_id = _short(message.get("message_id"))
    open_id = _short((sender.get("sender_id") or {}).get("open_id"))
    if not matched or message_id is None or open_id is None:
        return None
    # Binding keeps nothing to finish later, so it waits for a slot; if none frees up in
    # time the event goes unanswered and Feishu pushes it again.
    async with asyncio.timeout(BUDGET_SECONDS), slots or contextlib.nullcontext():
        result, user = await _bind(runtime, channel["id"], message_id, open_id, header, matched[1])
    if result is None and user is None:
        return None
    reply = BIND_REPLIES.get(result) or f"已绑定到 Keystone 用户 {user['label']}。"
    return {"kind": "text", "text": reply}, open_id, message_id


async def receive(runtime, channel, payload, reply, slots=None):
    """Handle one long-connection event for an app channel; returns the answer body.

    `reply(content, open_id, key)` sends a private answer after the event is acknowledged;
    `slots` is the Worker's semaphore bounding events processed at once.
    """
    try:
        body = json.loads(payload)
    except ValueError:
        return None
    header = body.get("header") if isinstance(body, dict) else None
    event = body.get("event") if isinstance(body, dict) else None
    if not isinstance(header, dict) or not isinstance(event, dict):
        return None
    if header.get("app_id") not in (None, channel["app_id"]):
        return None
    if header.get("event_type") == "card.action.trigger":
        return await receive_card_action(runtime, channel["id"], header, event, slots)
    if header.get("event_type") == "im.message.receive_v1":
        replied = await receive_message(runtime, channel, header, event, slots)
        if replied is not None:
            content, open_id, message_id = replied
            reply(content, open_id, message_id)
    return None


class SelfBindingService:
    """A signed-in user's own Feishu bindings: see them, bind by code, remove them."""

    def __init__(self, identity):
        self.identity = identity

    async def accounts(self):
        """Each active app the user can bind on, with the user's binding there if any."""
        async with self.identity.access(session_only=True) as (session, auth):
            rows = await session.execute(
                select(MessageChannel.id, MessageChannel.name, ExternalAccount)
                .outerjoin(
                    ExternalAccount,
                    (ExternalAccount.channel_id == MessageChannel.id)
                    & (ExternalAccount.user_id == auth.user.id),
                )
                .where(
                    MessageChannel.provider == "feishu_app",
                    MessageChannel.is_active.is_(True),
                )
                .order_by(MessageChannel.name, MessageChannel.id)
                .limit(50)
            )
            return [
                {
                    "channel_id": channel_id,
                    "channel_name": name,
                    "account": account
                    and {
                        "id": account.id,
                        "external_user_id": account.external_user_id,
                        "created_at": account.created_at,
                    },
                }
                for channel_id, name, account in rows
            ]

    async def create(self):
        """Issue the user's binding code; a new one replaces any earlier code."""
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
        expires_at = utcnow() + CODE_LIFETIME
        async with self.identity.access(session_only=True) as (session, auth):
            await session.execute(
                delete(MessageBindCode).where(MessageBindCode.user_id == auth.user.id)
            )
            session.add(
                MessageBindCode(
                    user_id=auth.user.id, code_digest=digest(code), expires_at=expires_at
                )
            )
        return {"code": code, "expires_at": expires_at}

    async def unbind(self, identifier):
        """Remove one of the user's own bindings; other users' bindings are not found."""
        action = "messaging.account.self_unbind"
        async with self.identity.access(
            session_only=True, action=action, target="messaging.account", target_id=identifier
        ) as (session, auth):
            row = await session.get(ExternalAccount, identifier, with_for_update=True)
            if row is None or row.user_id != auth.user.id:
                raise ServiceError(404, "message_account_not_found", "账号绑定不存在")
            details = {"channel_id": str(row.channel_id), "external_user_id": row.external_user_id}
            await session.delete(row)
            await session.flush()
            await self.identity.log(session, auth, action, "messaging.account", identifier, details)


class InboundService:
    def __init__(self, identity):
        self.identity = identity

    async def listing(self, offset, limit):
        async with self.identity.access("message_delivery:read") as (session, _):
            rows = (
                await session.scalars(
                    select(MessageInbound)
                    .order_by(MessageInbound.received_at.desc(), MessageInbound.id.desc())
                    .offset(offset)
                    .limit(limit)
                )
            ).all()
            users = {
                user["id"]: user["label"]
                for user in await users_in_transaction(
                    session, {row.actor_id for row in rows if row.actor_id}
                )
            }
            return [
                inbound_view(
                    row, row.actor_id and {"id": row.actor_id, "label": users.get(row.actor_id)}
                )
                for row in rows
            ]

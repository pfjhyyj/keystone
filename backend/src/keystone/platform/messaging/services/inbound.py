"""Inbound events: card actions and commands run as their declared executing user, binding
codes bind a user.

Events arrive on a Worker's long connection and are answered within Feishu's 3 seconds.
Each is stored once by its de-duplication key, then processed right away; one that does not
finish in time stays `received` and the Worker's periodic pass finishes it. Bodies, binding
codes and message text are never stored or logged; a command keeps only its matched key.
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

from sqlalchemy import delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert

from keystone.core.database import count_list
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
    MessageDestination,
    MessageInbound,
)
from keystone.platform.messaging.services.deliveries import register

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
UNBOUND_REPLY = "你的飞书账号还没有绑定 Keystone 用户，请先私聊应用发送绑定码"
EXPIRED_REPLY = "命令已过期，请重新发送"
FAILED_REPLY = "暂时无法处理，请稍后重新发送"
# Feishu replaces each mention in a group message's text with a key such as "@_user_1".
MENTION = re.compile(r"@_user_[0-9]+")


def digest(code):
    return hashlib.sha256(code.upper().encode()).hexdigest()


def inbound_view(row, actor=None):
    return {
        "id": row.id,
        "channel_id": row.channel_id,
        "kind": row.kind,
        "delivery_id": row.delivery_id,
        "action_key": row.action_key,
        "destination_id": row.destination_id,
        "command_module": row.command_module,
        "command_key": row.command_key,
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
    """An executing user's current role permissions; no PAT scope, nothing from the sender."""

    def __init__(self, runtime, user_id, correlation_id):
        super().__init__(runtime.database, runtime.settings, correlation_id=str(correlation_id))
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


# --- processing card actions and commands


async def _claim(runtime, inbound_id):
    """Take the processing lease; None while another processor holds it."""
    now = utcnow()
    async with runtime.database.transaction() as session:
        row = await session.get(MessageInbound, inbound_id, with_for_update=True)
        if row is None or row.status != "received":
            return None
        if row.attempts and row.updated_at > now - LEASE:
            return None
        if row.attempts >= MAX_ATTEMPTS:
            row.status, row.error_code, row.updated_at = "failed", "inbound_unfinished", now
            await _reply_failure(session, runtime, row, FAILED_REPLY)
            return None
        row.attempts, row.updated_at = row.attempts + 1, now
        return {
            key: getattr(row, key)
            for key in (
                "id",
                "kind",
                "channel_id",
                "delivery_id",
                "action_key",
                "destination_id",
                "command_module",
                "command_key",
                "provider_message_id",
                "external_user_id",
                "occurred_at",
                "received_at",
            )
        }


async def _reply_failure(session, runtime, row, text):
    """Answer a command the business never accepted, where its declaration asks for it."""
    if row.kind != "command":
        return
    declared = runtime.message_commands.get((row.command_module, row.command_key))
    if declared is None or not declared.reply_failures:
        return
    await register(
        session,
        row.actor_id,
        row.channel_id,
        {"kind": "text", "text": text[:4000]},
        {"source_module": "messaging", "source_type": "inbound", "source_id": str(row.id)},
        None,
        reply_inbound_id=row.id,
    )


async def _finish(runtime, inbound_id, status, error_code=None, actor_id=None, reply=None):
    now = utcnow()
    async with runtime.database.transaction() as session:
        row = await session.get(MessageInbound, inbound_id, with_for_update=True)
        if row is None or row.status != "received":
            return
        row.status, row.error_code, row.actor_id = status, error_code, actor_id
        row.updated_at, row.processed_at = now, now if status == "processed" else None
        if reply is not None:
            # Registered with the outcome, so a failure is answered exactly once.
            await _reply_failure(session, runtime, row, reply)


async def _executor(session, mode, channel_id, external_user_id, destination_id):
    """The Keystone user an event runs as: the sender's binding or the group's executing user."""
    if mode == "bound":
        user_id = await session.scalar(
            select(ExternalAccount.user_id).where(
                ExternalAccount.channel_id == channel_id,
                ExternalAccount.external_user_id == external_user_id,
            )
        )
        if user_id is None:
            raise Refused("message_actor_unbound", UNBOUND_REPLY)
        found = await users_in_transaction(session, [user_id])
        if not found or not found[0]["is_active"]:
            raise Refused("message_actor_disabled", "你绑定的 Keystone 用户已停用", user_id)
        return found[0]
    destination = destination_id and await session.get(MessageDestination, destination_id)
    if destination is None or destination.channel_id != channel_id or not destination.is_active:
        raise Refused("message_destination_unavailable", "这个群不能执行这项操作")
    if destination.executing_user_id is None:
        raise Refused("message_executor_unset", "这个群还没有配置执行用户，请联系管理员")
    found = await users_in_transaction(session, [destination.executing_user_id])
    if not found or not found[0]["is_active"]:
        # The executing user is not named to the group.
        raise Refused(
            "message_executor_disabled",
            "这个群的执行用户已停用，请联系管理员",
            destination.executing_user_id,
        )
    return found[0]


async def _authorized(runtime, user, permission, inbound_id):
    identity = ActingUser(runtime, user["id"], inbound_id)
    if permission:
        try:
            async with identity.access(permission):
                pass
        except ServiceError as error:
            raise Refused(error.code, "你没有执行这项操作的权限", user["id"]) from None
    return identity


async def _resolve_action(runtime, item):
    """Check the click against its delivery and find who it runs as."""
    async with runtime.database.transaction() as session:
        channel = await session.get(MessageChannel, item["channel_id"])
        delivery = item["delivery_id"] and await session.get(MessageDelivery, item["delivery_id"])
        actions = {
            action["key"]: action
            for action in (
                (delivery and delivery.content and delivery.content.get("actions")) or []
            )
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
        destination_id = delivery.destination_id
        if delivery.reply_inbound_id is not None:
            # A reply card belongs to the group the command came from.
            destination_id = await session.scalar(
                select(MessageInbound.destination_id).where(
                    MessageInbound.id == delivery.reply_inbound_id
                )
            )
        user = await _executor(
            session, declared.identity, item["channel_id"], item["external_user_id"], destination_id
        )
        return declared, delivery, actions[item["action_key"]], user


async def _refused(runtime, item, refused, reply):
    await _finish(
        runtime,
        item["id"],
        "rejected",
        refused.code,
        refused.actor_id,
        reply=refused.message if reply else None,
    )
    await audit.failed(
        runtime.database,
        f"messaging.inbound.{item['kind']}",
        Actor("user", refused.actor_id) if refused.actor_id else Actor("system"),
        "messaging.inbound",
        item["id"],
        str(item["id"]),
        refused.code,
        True,
    )
    return {"status": "rejected", "message": refused.message}


async def _process_action(runtime, item):
    inbound_id = item["id"]
    try:
        declared, delivery, action, user = await _resolve_action(runtime, item)
        identity = await _authorized(runtime, user, declared.permission, inbound_id)
        try:
            await declared.handler(runtime, identity, delivery.source_id, action["key"], inbound_id)
        except ServiceError as error:
            # A business refusal is the business's to audit; only the outcome is kept here.
            await _finish(runtime, inbound_id, "rejected", error.code, user["id"])
            return {"status": "rejected", "message": error.detail}
    except Refused as refused:
        return await _refused(runtime, item, refused, False)
    await _finish(runtime, inbound_id, "processed", actor_id=user["id"])
    card = feishu_content.action_card(
        delivery.content, delivery.id, outcome=f"{user['label']} 已{action['text']}"
    )
    return {"status": "processed", "card": card, "done": f"已{action['text']}"}


async def _resolve_command(runtime, item, declared):
    """Check the command is still accepted here and in time; None once it has expired."""
    async with runtime.database.transaction() as session:
        if declared is None:
            raise Refused("message_command_unknown", "这个命令已不再支持")
        channel = await session.get(MessageChannel, item["channel_id"])
        if not channel.is_active:
            raise Refused("message_channel_disabled", "该应用已停用")
        place = "group" if item["destination_id"] else "p2p"
        if place not in declared.where:
            raise Refused("message_command_mismatch", "这个命令不能在这里使用")
        if place == "group":
            destination = await session.get(MessageDestination, item["destination_id"])
            if (
                destination is None
                or destination.channel_id != item["channel_id"]
                or not destination.is_active
            ):
                raise Refused("message_destination_unavailable", "这个群不能执行这项操作")
        occurred = item["occurred_at"] or item["received_at"]
        if utcnow() - occurred > declared.accept_within:
            return None
        return await _executor(
            session,
            declared.identity,
            item["channel_id"],
            item["external_user_id"],
            item["destination_id"],
        )


async def _process_command(runtime, item):
    inbound_id = item["id"]
    declared = runtime.message_commands.get((item["command_module"], item["command_key"]))
    reply = bool(declared and declared.reply_failures)
    try:
        user = await _resolve_command(runtime, item, declared)
        if user is None:
            await _finish(runtime, inbound_id, "expired", reply=EXPIRED_REPLY if reply else None)
            return {"status": "expired"}
        identity = await _authorized(runtime, user, declared.permission, inbound_id)
        try:
            await declared.handler(identity, inbound_id, declared.key)
        except ServiceError as error:
            await _finish(
                runtime,
                inbound_id,
                "rejected",
                error.code,
                user["id"],
                reply=error.detail if reply else None,
            )
            return {"status": "rejected"}
    except Refused as refused:
        return await _refused(runtime, item, refused, reply)
    await _finish(runtime, inbound_id, "processed", actor_id=user["id"])
    return {"status": "processed"}


async def process(runtime, inbound_id):
    """Process one claimed card action or command; returns the outcome, or None if not claimed."""
    item = await _claim(runtime, inbound_id)
    if item is None:
        return None
    if item["kind"] == "command":
        return await _process_command(runtime, item)
    return await _process_action(runtime, item)


async def _inline(runtime, inbound_id, slots):
    """Process right away within the budget; None leaves it to the periodic pass."""
    if slots is not None and slots.locked():
        return None
    try:
        async with slots or contextlib.nullcontext(), asyncio.timeout(BUDGET_SECONDS):
            return await process(runtime, inbound_id)
    except TimeoutError:
        # Cancelled mid-way: the business transaction rolled back, the lease expires and the
        # periodic pass processes it again.
        return None


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
    return _card_answer(await _inline(runtime, inbound_id, slots))


async def retry(runtime, vault):
    """Finish events that inline processing did not; returns how many were tried."""
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
        try:
            outcome = await process(runtime, inbound_id)
        except Exception as error:
            # Left `received`; the lease runs out and a later pass tries again.
            log.warning(
                "messaging_inbound_failed inbound=%s type=%s", inbound_id, type(error).__name__
            )
            continue
        if not outcome or "card" not in outcome or not message_id:
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


def _command(runtime, text, place):
    return next(
        (
            command
            for command in runtime.message_commands.values()
            if command.text == text and place in command.where
        ),
        None,
    )


async def _group_destination(database, channel_id, chat_id):
    if not isinstance(chat_id, str):
        return None
    async with database.transaction() as session:
        return await session.scalar(
            select(MessageDestination.id).where(
                MessageDestination.channel_id == channel_id,
                MessageDestination.external_id == chat_id,
                MessageDestination.is_active.is_(True),
            )
        )


async def receive_message(runtime, channel, header, event, slots=None):
    """Handle private binding codes and declared commands; anything else is ignored unstored.

    Returns (reply content, open ID, message ID) for a binding message, otherwise None.
    """
    message = event.get("message") or {}
    sender = event.get("sender") or {}
    if message.get("message_type") != "text" or sender.get("sender_type") != "user":
        return None
    try:
        text = json.loads(message.get("content") or "").get("text")
    except (ValueError, AttributeError):
        return None
    message_id = _short(message.get("message_id"))
    open_id = _short((sender.get("sender_id") or {}).get("open_id"))
    if not isinstance(text, str) or message_id is None or open_id is None:
        return None
    destination_id = None
    if message.get("chat_type") == "p2p":
        matched = BIND_MESSAGE.fullmatch(text)
        if matched:
            return await _receive_bind(
                runtime, channel, header, message_id, open_id, matched, slots
            )
        command = _command(runtime, text.strip(), "p2p")
    elif message.get("chat_type") == "group" and message.get("mentions"):
        # Group messages reach the app only when it is mentioned; mentions are not the text.
        command = _command(runtime, MENTION.sub("", text).strip(), "group")
        if command is not None:
            destination_id = await _group_destination(
                runtime.database, channel["id"], message.get("chat_id")
            )
            if destination_id is None:
                return None
    else:
        return None
    if command is None:
        return None
    inbound_id, _, new = await _store(
        runtime.database,
        channel["id"],
        dedupe_key=message_id,
        kind="command",
        destination_id=destination_id,
        command_module=command.module,
        command_key=command.key,
        provider_message_id=message_id,
        external_user_id=open_id,
        occurred_at=_occurred(header),
    )
    if new:
        # Results are answered by replies the handler or the platform registers.
        await _inline(runtime, inbound_id, slots)
    return None


async def _receive_bind(runtime, channel, header, message_id, open_id, matched, slots):
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

    async def listing(self, offset, limit, channel_id=None, status=None, *, total=None):
        from keystone.platform.messaging.services.deliveries import filters

        async with self.identity.access("message_delivery:read") as (session, _):
            statement = (
                select(MessageInbound)
                .where(*filters(MessageInbound, channel_id, status))
                .order_by(MessageInbound.received_at.desc(), MessageInbound.id.desc())
                .offset(offset)
                .limit(limit)
            )
            await count_list(session, statement, total)
            rows = (await session.scalars(statement)).all()
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

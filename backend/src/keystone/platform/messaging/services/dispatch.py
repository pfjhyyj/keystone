"""Worker sending: claim, send outside the transaction, record the outcome.

Group bots offer no de-duplication, so their claimed deliveries are sent at most once: an
interrupted or unanswered send ends as `unknown` for good. App channels send with the
delivery ID as Feishu's uuid, so an unknown outcome is retried with the same ID while the
provider window still covers it.

Supplied content is fetched from the business right before each attempt and goes straight
to the provider; it is never stored, logged or kept in an error.
"""

import asyncio
from datetime import timedelta

from pydantic import ValidationError
from sqlalchemy import and_, func, or_, select, update

from keystone.core.errors import ServiceError
from keystone.core.time import utcnow
from keystone.platform.identity.services.subjects import users_in_transaction
from keystone.platform.messaging.adapters import feishu_app, feishu_webhook
from keystone.platform.messaging.models import (
    ExternalAccount,
    MessageChannel,
    MessageDelivery,
    MessageDestination,
    MessageInbound,
)
from keystone.platform.messaging.services.channels import unseal
from keystone.platform.messaging.services.deliveries import checked_content
from keystone.platform.messaging.services.inbound import ActingUser

BATCH = 20
# A send never takes longer than this; an older `sending` row lost its process.
STALE_SENDING = timedelta(minutes=2)
SEND_INTERVAL = 0.2
MAX_ATTEMPTS = 3
RETRY_DELAY = timedelta(seconds=30)
RETRY_WINDOW = timedelta(seconds=feishu_app.DEDUPE_WINDOW_SECONDS)
# A content provider is bounded so one slow business cannot hold the whole pass.
SUPPLY_SECONDS = 10


class Final(Exception):
    """The delivery cannot be sent at all; it fails without reaching the provider."""

    def __init__(self, code):
        self.code = code


async def _settle(database, now):
    async with database.transaction() as session:
        await session.execute(
            update(MessageDelivery)
            .where(
                MessageDelivery.status == "pending",
                MessageDelivery.expires_at.is_not(None),
                MessageDelivery.expires_at <= now,
            )
            .values(status="expired", updated_at=now)
        )
        await session.execute(
            update(MessageDelivery)
            .where(
                MessageDelivery.status == "sending",
                MessageDelivery.updated_at < now - STALE_SENDING,
            )
            .values(status="unknown", error_code="send_interrupted", updated_at=now)
        )


async def _claim(database, now):
    retryable = and_(
        MessageDelivery.status == "unknown",
        MessageChannel.provider == "feishu_app",
        MessageDelivery.attempts < MAX_ATTEMPTS,
        MessageDelivery.first_attempted_at > now - RETRY_WINDOW,
        MessageDelivery.updated_at < now - RETRY_DELAY,
    )
    async with database.transaction() as session:
        rows = list(
            await session.execute(
                select(
                    MessageDelivery,
                    MessageDestination.external_id,
                    ExternalAccount.external_user_id,
                    MessageInbound.provider_message_id,
                )
                .join(MessageChannel, MessageDelivery.channel_id == MessageChannel.id)
                .outerjoin(
                    MessageDestination, MessageDelivery.destination_id == MessageDestination.id
                )
                # The binding is read now, so a user unbound after registration is not reached.
                .outerjoin(
                    ExternalAccount,
                    and_(
                        ExternalAccount.channel_id == MessageDelivery.channel_id,
                        ExternalAccount.user_id == MessageDelivery.recipient_user_id,
                    ),
                )
                .outerjoin(MessageInbound, MessageInbound.id == MessageDelivery.reply_inbound_id)
                .where(
                    or_(MessageDelivery.status == "pending", retryable),
                    or_(MessageDelivery.expires_at.is_(None), MessageDelivery.expires_at > now),
                    MessageChannel.is_active.is_(True),
                    or_(
                        MessageDelivery.destination_id.is_(None),
                        MessageDestination.is_active.is_(True),
                    ),
                )
                .order_by(MessageDelivery.created_at, MessageDelivery.id)
                .limit(BATCH)
                .with_for_update(of=MessageDelivery, skip_locked=True)
            )
        )
        active = {
            user["id"]
            for user in await users_in_transaction(
                session, {row.recipient_user_id for row, *_ in rows if row.recipient_user_id}
            )
            if user["is_active"]
        }
        claimed = []
        for row, chat_id, open_id, message_id in rows:
            row.updated_at = now
            if row.recipient_user_id and (open_id is None or row.recipient_user_id not in active):
                # Never redirected to someone else: the delivery ends without a send.
                row.status = "failed"
                row.error_code = "recipient_unbound" if open_id is None else "recipient_disabled"
                continue
            if row.reply_inbound_id and message_id is None:
                row.status, row.error_code = "failed", "reply_target_missing"
                continue
            row.status, row.attempts = "sending", row.attempts + 1
            row.first_attempted_at = row.first_attempted_at or now
            if row.reply_inbound_id:
                target = ("reply", message_id)
            elif row.recipient_user_id:
                target = ("open_id", open_id)
            else:
                target = ("chat_id", chat_id)
            claimed.append(
                {
                    "id": row.id,
                    "channel_id": row.channel_id,
                    "target": target,
                    "kind": row.kind,
                    "content": row.content,
                    "attempts": row.attempts,
                    "source": (row.source_module, row.source_type, row.source_id),
                    "created_by": row.created_by,
                }
            )
        return claimed


async def _record(database, delivery_id, status, error_code=None, message_id=None):
    now = utcnow()
    values = {"status": status, "error_code": error_code, "updated_at": now}
    if status == "succeeded":
        values |= {"sent_at": func.coalesce(MessageDelivery.sent_at, now)}
        values |= {"provider_message_id": message_id}
    async with database.transaction() as session:
        await session.execute(
            update(MessageDelivery)
            .where(MessageDelivery.id == delivery_id, MessageDelivery.status == "sending")
            .values(**values)
        )


async def _supply(runtime, item):
    """Ask the source's provider for the content as the delivery's creator; None if gone."""
    source_module, source_type, source_id = item["source"]
    provider = runtime.message_contents.get((source_module, source_type))
    if provider is None:
        raise Final("content_provider_missing")
    if item["created_by"] is None:
        raise Final("content_owner_missing")
    identity = ActingUser(runtime, item["created_by"], item["id"])
    try:
        async with asyncio.timeout(SUPPLY_SECONDS):
            content = await provider(identity, source_id)
    except ServiceError as error:
        raise Final(error.code) from None
    except Exception:
        # Nothing reached the provider; the error itself may quote the content.
        raise feishu_app.NotSent("content_provider_failed") from None
    if content is None:
        return None
    try:
        content = checked_content(content)
    except (ValidationError, ServiceError):
        raise Final("content_invalid") from None
    # Actions are stored with the delivery so clicks can be checked; supplied cards have none.
    if content["kind"] != item["kind"] or content.get("actions"):
        raise Final("content_invalid")
    return content


async def _send(channel, credential, target, content, delivery_id):
    """Return the provider message ID, or None where the channel gives none."""
    if channel.provider == "feishu_webhook":
        await feishu_webhook.send(credential["url"], credential.get("secret"), content)
        return None
    receive_id_type, receive_id = target
    if receive_id_type == "reply":
        return await feishu_app.reply(
            channel.app_id, credential["app_secret"], receive_id, content, str(delivery_id)
        )
    return await feishu_app.send(
        channel.app_id,
        credential["app_secret"],
        receive_id_type,
        receive_id,
        content,
        str(delivery_id),
    )


async def dispatch(runtime, vault):
    """Send one batch; returns how many deliveries were attempted."""
    database = runtime.database
    now = utcnow()
    await _settle(database, now)
    claimed = await _claim(database, now)
    channels = {}
    for index, item in enumerate(claimed):
        delivery_id, channel_id, attempts = item["id"], item["channel_id"], item["attempts"]
        if index:
            await asyncio.sleep(SEND_INTERVAL)
        if channel_id not in channels:
            async with database.transaction() as session:
                channels[channel_id] = await session.get(MessageChannel, channel_id)
        channel = channels[channel_id]
        try:
            credential = unseal(vault, channel)
        except ServiceError:
            await _record(database, delivery_id, "failed", "credential_unavailable")
            continue
        try:
            content = item["content"]
            if content is None:
                content = await _supply(runtime, item)
                if content is None:
                    await _record(database, delivery_id, "expired", "content_unavailable")
                    continue
            message_id = await _send(channel, credential, item["target"], content, delivery_id)
        except Final as error:
            await _record(database, delivery_id, "failed", error.code)
        except feishu_app.NotSent as error:
            # Nothing was delivered by this attempt; an earlier one may have been, though.
            status = "pending" if attempts == 1 else "unknown"
            await _record(database, delivery_id, status, error.code)
        except feishu_webhook.Rejected as error:
            await _record(database, delivery_id, "failed", error.code)
        except feishu_webhook.Unknown:
            await _record(database, delivery_id, "unknown", "send_unconfirmed")
        else:
            await _record(database, delivery_id, "succeeded", message_id=message_id)
    return len(claimed)

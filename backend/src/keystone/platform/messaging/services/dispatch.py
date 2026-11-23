"""Worker sending: claim, send outside the transaction, record the outcome.

Group bots offer no de-duplication, so their claimed deliveries are sent at most once: an
interrupted or unanswered send ends as `unknown` for good. App channels send with the
delivery ID as Feishu's uuid, so an unknown outcome is retried with the same ID while the
provider window still covers it.
"""

import asyncio
from datetime import timedelta

from sqlalchemy import and_, func, or_, select, update

from keystone.core.errors import ServiceError
from keystone.core.time import utcnow
from keystone.platform.messaging.adapters import feishu_app, feishu_webhook
from keystone.platform.messaging.models import MessageChannel, MessageDelivery, MessageDestination
from keystone.platform.messaging.services.channels import unseal

BATCH = 20
# A send never takes longer than this; an older `sending` row lost its process.
STALE_SENDING = timedelta(minutes=2)
SEND_INTERVAL = 0.2
MAX_ATTEMPTS = 3
RETRY_DELAY = timedelta(seconds=30)
RETRY_WINDOW = timedelta(seconds=feishu_app.DEDUPE_WINDOW_SECONDS)


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
                select(MessageDelivery, MessageDestination.external_id)
                .join(MessageChannel, MessageDelivery.channel_id == MessageChannel.id)
                .join(MessageDestination, MessageDelivery.destination_id == MessageDestination.id)
                .where(
                    or_(MessageDelivery.status == "pending", retryable),
                    or_(MessageDelivery.expires_at.is_(None), MessageDelivery.expires_at > now),
                    MessageChannel.is_active.is_(True),
                    MessageDestination.is_active.is_(True),
                )
                .order_by(MessageDelivery.created_at, MessageDelivery.id)
                .limit(BATCH)
                .with_for_update(of=MessageDelivery, skip_locked=True)
            )
        )
        claimed = []
        for row, external_id in rows:
            row.status, row.attempts, row.updated_at = "sending", row.attempts + 1, now
            row.first_attempted_at = row.first_attempted_at or now
            claimed.append((row.id, row.channel_id, external_id, row.content, row.attempts))
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


async def _send(channel, credential, external_id, content, delivery_id):
    """Return the provider message ID, or None where the channel gives none."""
    if channel.provider == "feishu_webhook":
        await feishu_webhook.send(credential["url"], credential.get("secret"), content)
        return None
    return await feishu_app.send(
        channel.app_id,
        credential["app_secret"],
        "chat_id",
        external_id,
        content,
        str(delivery_id),
    )


async def dispatch(database, vault):
    """Send one batch; returns how many deliveries were attempted."""
    now = utcnow()
    await _settle(database, now)
    claimed = await _claim(database, now)
    channels = {}
    for index, (delivery_id, channel_id, external_id, content, attempts) in enumerate(claimed):
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
            message_id = await _send(channel, credential, external_id, content, delivery_id)
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

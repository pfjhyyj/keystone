"""Worker sending: claim, send outside the transaction, record the outcome.

Group bots offer no de-duplication, so a claimed delivery is sent at most once: an
interrupted or unanswered send ends as `unknown` and is never retried automatically.
"""

import asyncio
from datetime import timedelta

from sqlalchemy import or_, select, update

from keystone.core.errors import ServiceError
from keystone.core.time import utcnow
from keystone.platform.messaging.adapters import feishu_webhook
from keystone.platform.messaging.models import MessageChannel, MessageDelivery, MessageDestination
from keystone.platform.messaging.services.channels import unseal

BATCH = 20
# A send never takes longer than this; an older `sending` row lost its process.
STALE_SENDING = timedelta(minutes=2)
SEND_INTERVAL = 0.2


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
    async with database.transaction() as session:
        rows = list(
            await session.scalars(
                select(MessageDelivery)
                .join(MessageChannel, MessageDelivery.channel_id == MessageChannel.id)
                .join(MessageDestination, MessageDelivery.destination_id == MessageDestination.id)
                .where(
                    MessageDelivery.status == "pending",
                    or_(MessageDelivery.expires_at.is_(None), MessageDelivery.expires_at > now),
                    MessageChannel.is_active.is_(True),
                    MessageDestination.is_active.is_(True),
                )
                .order_by(MessageDelivery.created_at, MessageDelivery.id)
                .limit(BATCH)
                .with_for_update(of=MessageDelivery, skip_locked=True)
            )
        )
        for row in rows:
            row.status, row.attempts, row.updated_at = "sending", row.attempts + 1, now
        return [(row.id, row.channel_id, row.content) for row in rows]


async def _record(database, delivery_id, status, error_code=None):
    now = utcnow()
    async with database.transaction() as session:
        await session.execute(
            update(MessageDelivery)
            .where(MessageDelivery.id == delivery_id, MessageDelivery.status == "sending")
            .values(
                status=status,
                error_code=error_code,
                updated_at=now,
                **({"sent_at": now} if status == "succeeded" else {}),
            )
        )


async def dispatch(database, vault):
    """Send one batch; returns how many deliveries reached a final outcome."""
    now = utcnow()
    await _settle(database, now)
    claimed = await _claim(database, now)
    channels = {}
    for index, (delivery_id, channel_id, content) in enumerate(claimed):
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
            await feishu_webhook.send(credential["url"], credential.get("secret"), content)
        except feishu_webhook.Rejected as error:
            await _record(database, delivery_id, "failed", error.code)
        except feishu_webhook.Unknown:
            await _record(database, delivery_id, "unknown", "send_unconfirmed")
        else:
            await _record(database, delivery_id, "succeeded")
    return len(claimed)

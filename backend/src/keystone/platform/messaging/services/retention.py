"""Delivery retention: finished deliveries go after the retention period; open ones stay."""

from datetime import timedelta

from sqlalchemy import delete, select

from keystone.core.time import utcnow
from keystone.platform.messaging.models import MessageDelivery


async def prune_deliveries(database, days, *, apply=False, limit=200):
    cutoff = utcnow() - timedelta(days=days)
    async with database.transaction() as session:
        # A row is last touched when it ends, and an `unknown` row this old is past every
        # retry window. created_at never exceeds updated_at, so it only narrows the index scan.
        query = (
            select(MessageDelivery.id)
            .where(
                MessageDelivery.status.not_in(("pending", "sending")),
                MessageDelivery.created_at < cutoff,
                MessageDelivery.updated_at < cutoff,
            )
            .order_by(MessageDelivery.created_at, MessageDelivery.id)
            .limit(limit)
        )
        if apply:
            query = query.with_for_update(skip_locked=True)
        identifiers = list(await session.scalars(query))
        if apply and identifiers:
            await session.execute(
                delete(MessageDelivery).where(MessageDelivery.id.in_(identifiers))
            )
        return len(identifiers)

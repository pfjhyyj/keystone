"""Messaging retention: finished deliveries and inbound events go after the retention period.

Open records stay however old: pending or sending deliveries and received inbound events.
Expired binding codes are useless and go at once.
"""

from datetime import timedelta

from sqlalchemy import delete, select

from keystone.core.time import utcnow
from keystone.platform.messaging.models import MessageBindCode, MessageDelivery, MessageInbound


async def _prune(session, model, created, open_statuses, cutoff, apply, limit):
    # A row is last touched when it ends; an `unknown` delivery this old is past every retry
    # window. The creation time never exceeds updated_at, so it only narrows the index scan.
    query = (
        select(model.id)
        .where(
            model.status.not_in(open_statuses),
            created < cutoff,
            model.updated_at < cutoff,
        )
        .order_by(created, model.id)
        .limit(limit)
    )
    if apply:
        query = query.with_for_update(skip_locked=True)
    identifiers = list(await session.scalars(query))
    if apply and identifiers:
        await session.execute(delete(model).where(model.id.in_(identifiers)))
    return len(identifiers)


async def prune_messaging(database, days, *, apply=False, limit=200):
    cutoff = utcnow() - timedelta(days=days)
    async with database.transaction() as session:
        result = {
            "message_deliveries": await _prune(
                session,
                MessageDelivery,
                MessageDelivery.created_at,
                ("pending", "sending"),
                cutoff,
                apply,
                limit,
            ),
            "message_inbounds": await _prune(
                session,
                MessageInbound,
                MessageInbound.received_at,
                ("received",),
                cutoff,
                apply,
                limit,
            ),
        }
        if apply:
            await session.execute(
                delete(MessageBindCode).where(MessageBindCode.expires_at <= utcnow())
            )
        return result

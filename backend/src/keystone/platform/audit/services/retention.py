"""Audit retention is operator maintenance; it never audits its own deletions."""

from datetime import timedelta

from sqlalchemy import delete, select

from keystone.core.time import utcnow
from keystone.platform.audit.models import AuditLog


async def prune_audit(database, days, *, apply=False, limit=200):
    async with database.transaction() as session:
        query = (
            select(AuditLog.id)
            .where(AuditLog.occurred_at < utcnow() - timedelta(days=days))
            .order_by(AuditLog.occurred_at, AuditLog.id)
            .limit(limit)
        )
        if apply:
            query = query.with_for_update(skip_locked=True)
        identifiers = list(await session.scalars(query))
        if apply and identifiers:
            await session.execute(delete(AuditLog).where(AuditLog.id.in_(identifiers)))
        return len(identifiers)

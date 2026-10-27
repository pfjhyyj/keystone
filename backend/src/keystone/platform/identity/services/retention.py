"""Bounded credential retention, coordinated with identity changes and Flow references."""

from datetime import timedelta

from sqlalchemy import delete, func, select

from keystone.core.time import utcnow
from keystone.platform.identity.models import LoginSession, PersonalAccessToken
from keystone.platform.identity.services.authorization import identity_lock


async def prune_credentials(database, settings, retained_tokens, *, apply=False, limit=200):
    counts = {}
    async with database.transaction() as session:
        await identity_lock(session, identity_write=apply)
        for key, model, days in (
            ("sessions", LoginSession, settings.session_retention_days),
            ("tokens", PersonalAccessToken, settings.pat_retention_days),
        ):
            inactive_at = func.least(model.expires_at, model.revoked_at)
            query = select(model.id).where(inactive_at < utcnow() - timedelta(days=days))
            if model is PersonalAccessToken:
                query = query.where(model.id.not_in(retained_tokens()))
            query = query.order_by(inactive_at, model.id).limit(limit)
            if apply:
                query = query.with_for_update(skip_locked=True)
            identifiers = list(await session.scalars(query))
            counts[key] = len(identifiers)
            if apply and identifiers:
                await session.execute(delete(model).where(model.id.in_(identifiers)))
    return counts

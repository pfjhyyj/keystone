"""Shared send eligibility, committed with the original registration transaction."""

import re
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from keystone.platform.model_access.models import ModelDispatchScope


class ModelDispatchService:
    def __init__(self, database, settings):
        self.database, self.settings = database, settings

    @staticmethod
    async def locked(session, scope):
        if not isinstance(scope, str) or not re.fullmatch(r"[0-9a-f]{64}", scope):
            raise ValueError("Invalid credential scope")
        await session.execute(
            insert(ModelDispatchScope).values(scope=scope).on_conflict_do_nothing()
        )
        return await session.get(ModelDispatchScope, scope, with_for_update=True)

    async def claim_in_transaction(self, session, scope):
        row = await self.locked(session, scope)
        now = await session.scalar(select(func.clock_timestamp()))
        delay = (max(row.next_send_at, row.blocked_until) - now).total_seconds()
        if delay > 0:
            return delay
        row.next_send_at = now + timedelta(milliseconds=self.settings.model_send_interval_ms)
        row.updated_at = now
        return 0

    async def cooldown(self, scope, seconds):
        seconds = self.settings.model_cooldown_seconds if seconds is None else seconds
        seconds = max(1, min(seconds, self.settings.model_cooldown_max_seconds))
        async with self.database.transaction() as session:
            row = await self.locked(session, scope)
            now = await session.scalar(select(func.clock_timestamp()))
            row.blocked_until = max(row.blocked_until, now + timedelta(seconds=seconds))
            row.updated_at = now

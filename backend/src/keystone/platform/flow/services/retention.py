"""Bounded retention for expired terminal Flow step-diagnostic artifacts."""

from datetime import timedelta

from sqlalchemy import String, cast, func, select

from keystone.core.time import utcnow
from keystone.platform.files.services.references import (
    diagnostic_owners,
    release_diagnostic_files_in_transaction,
)
from keystone.platform.flow.models import TERMINAL, FlowRun
from keystone.platform.flow.schemas.step_diagnostics import DIAGNOSTIC_SLOT_PREFIX


async def prune_flow_diagnostics(database, days, *, apply=False, limit=200):
    cutoff = utcnow() - timedelta(days=days)
    async with database.transaction() as session:
        owner_expr = func.concat("flow:", cast(FlowRun.id, String))
        query = (
            select(FlowRun.id)
            .where(
                FlowRun.status.in_(TERMINAL),
                FlowRun.finished_at < cutoff,
                owner_expr.in_(diagnostic_owners(DIAGNOSTIC_SLOT_PREFIX)),
            )
            .order_by(FlowRun.finished_at, FlowRun.id)
            .limit(limit)
        )
        if apply:
            query = query.with_for_update(skip_locked=True)
        run_ids = list(await session.scalars(query))
        if not run_ids:
            return 0
        owners = [f"flow:{run_id}" for run_id in run_ids]
        return await release_diagnostic_files_in_transaction(
            session,
            owners,
            DIAGNOSTIC_SLOT_PREFIX,
            apply=apply,
        )

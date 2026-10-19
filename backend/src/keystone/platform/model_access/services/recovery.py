from sqlalchemy import select

from keystone.core.time import utcnow
from keystone.platform.audit.services import writer as audit
from keystone.platform.model_access.models import ModelInvocation


async def recover_interrupted_calls(database):
    # Only at startup after exclusive Worker lock: the previous process is known to have ended.
    return await recover_calls(database, flow=True, source="flow_worker_recovery")


async def recover_calls(database, *, flow, source):
    """Internal batch operation; entrypoints must establish the process-stop precondition."""
    context = ModelInvocation.flow_context["flow_run_id"].astext
    count = 0
    while True:
        async with database.transaction() as session:
            rows = list(
                await session.scalars(
                    select(ModelInvocation)
                    .where(
                        ModelInvocation.status == "running",
                        context.is_not(None) if flow else context.is_(None),
                    )
                    .order_by(ModelInvocation.id)
                    .limit(100)
                    .with_for_update()
                )
            )
            if not rows:
                return count
            now = utcnow()
            for row in rows:
                row.status, row.error_code = "unknown", "model_interrupted"
                row.finished_at = row.updated_at = now
                await audit.record(
                    session,
                    "model.invocation.recover",
                    audit.Actor("system"),
                    "model.invocation",
                    row.id,
                    details={"source": source},
                )
        count += len(rows)

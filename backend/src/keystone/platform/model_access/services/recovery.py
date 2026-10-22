from sqlalchemy import select

from keystone.core.errors import ServiceError
from keystone.core.time import utcnow
from keystone.platform.audit.services import writer as audit
from keystone.platform.model_access.models import ModelInvocation
from keystone.platform.model_access.services.cost_policy import initialize_check


async def has_worker_calls(session, worker_id):
    return (
        await session.scalar(
            select(ModelInvocation.id)
            .where(ModelInvocation.worker_id == worker_id, ModelInvocation.status == "running")
            .limit(1)
        )
        is not None
    )


async def recover_interrupted_calls(database, owner, *, lease):
    # Only at startup after exclusive Worker lock: the previous process is known to have ended.
    async with lease.transaction() as session:
        if await session.scalar(
            select(ModelInvocation.id)
            .where(
                ModelInvocation.status == "running",
                ModelInvocation.flow_context["flow_run_id"].astext.is_not(None),
                ModelInvocation.worker_id.is_(None),
            )
            .limit(1)
        ):
            raise ServiceError(
                409, "legacy_flow_recovery_required", "旧 Flow 调用无执行归属，请停机后执行专项收尾"
            )
    return await recover_calls(
        database, flow=True, source="flow_worker_recovery", owner=owner, lease=lease
    )


async def recover_calls(database, *, flow, source, owner=None, legacy_only=False, lease=None):
    """Internal batch operation; entrypoints must establish the process-stop precondition."""
    context = ModelInvocation.flow_context["flow_run_id"].astext
    count = 0
    while True:
        async with lease.transaction() if lease else database.transaction() as session:
            rows = list(
                await session.scalars(
                    select(ModelInvocation)
                    .where(
                        ModelInvocation.status == "running",
                        context.is_not(None) if flow else context.is_(None),
                        *(
                            [
                                ModelInvocation.worker_id == owner.worker_id,
                                ModelInvocation.worker_instance_id != owner.instance_id,
                            ]
                            if owner
                            else []
                        ),
                        *([ModelInvocation.worker_id.is_(None)] if legacy_only else []),
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
                initialize_check(row, now)
                await audit.record(
                    session,
                    "model.invocation.recover",
                    audit.Actor("system"),
                    "model.invocation",
                    row.id,
                    details={"source": source},
                )
        count += len(rows)

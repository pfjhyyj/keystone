"""Use native DBOS APIs to prune only old, completed maintenance leaves."""

from datetime import timedelta

from dbos import DBOS, DBOSClient
from sqlalchemy import create_engine

from keystone.core.asyncio import blocking
from keystone.core.execution.contracts import (
    NoInput,
    ScheduleDefinition,
    TaskBinding,
    TaskDefinition,
)
from keystone.core.execution.schedules import register_schedule
from keystone.core.execution.steps import safe_step
from keystone.core.time import utcnow

TERMINAL = {"SUCCESS", "ERROR", "CANCELLED", "MAX_RECOVERY_ATTEMPTS_EXCEEDED"}
HISTORY = TaskDefinition(
    key="history.retention",
    entrypoint="keystone.history.retention",
    input_model=NoInput,
    timeout_seconds=60,
    queue="keystone-history-maintenance",
)
HISTORY_SCHEDULE = ScheduleDefinition(
    name="keystone.history.retention",
    tick_entrypoint="keystone.history.tick",
    task_key=HISTORY.key,
    cron="0 * * * * *",
)
QUEUE = HISTORY.queue


def register(execute, *, enabled):
    @safe_step(name="history.retention", error_code="history_retention_failed")
    async def prune():
        return await execute()

    @DBOS.workflow(name=HISTORY.entrypoint)
    async def retention(payload):
        try:
            NoInput.model_validate(payload)
            if enabled:
                await prune()
        except Exception:
            raise RuntimeError("history_retention_failed") from None

    task = TaskBinding(HISTORY, retention, enabled)
    return (task,), register_schedule(HISTORY_SCHEDULE, task, execution_prefix="history-retention")


async def prune_maintenance(settings, names, retained_ids, *, apply=False, limit=200, offset=0):
    if not 1 <= limit <= 200 or not 0 <= offset <= 1_000_000_000:
        raise ValueError("Invalid maintenance history page")
    cutoff = utcnow() - timedelta(days=settings.maintenance_retention_days)
    engine = create_engine(
        settings.database_url.get_secret_value(),
        pool_size=1,
        max_overflow=0,
        connect_args={
            "connect_timeout": 3,
            "options": "-cstatement_timeout=3000 -clock_timeout=3000",
        },
        hide_parameters=True,
    )
    client = DBOSClient(
        system_database_engine=engine,
        application_name="keystone",
        lazy=True,
        retry_connection_errors=False,
        observability_query_timeout_sec=3,
    )

    async def listing(**options):
        from functools import partial

        return await blocking(
            partial(client.list_workflows, load_input=False, load_output=False, **options)
        )

    try:
        rows = await listing(
            application_name="keystone",
            name=list(names),
            status=list(TERMINAL),
            completed_before=cutoff.isoformat(),
            limit=limit,
            offset=offset,
        )
        known = {row.workflow_id: row for row in rows}
        missing = set()
        # Ancestors must also be old maintenance, never a business Run or an active parent.
        for _ in range(16):
            parents = (
                {r.parent_workflow_id for r in known.values() if r.parent_workflow_id}
                - known.keys()
                - missing
            )
            if not parents:
                break
            if len(known) + len(parents) > limit * 4:
                break
            loaded = await listing(workflow_ids=sorted(parents), limit=len(parents))
            known.update((row.workflow_id, row) for row in loaded)
            missing.update(parents - {row.workflow_id for row in loaded})
        protected = await retained_ids(set(known))

        def safe(row):
            visited = set()
            while row is not None and row.workflow_id not in visited and len(visited) < 16:
                visited.add(row.workflow_id)
                if (
                    row.workflow_id in protected
                    or row.application_name != "keystone"
                    or row.name not in names
                    or row.status not in TERMINAL
                    or row.forked_from
                    or row.was_forked_from
                    or row.completed_at is None
                    or row.completed_at >= cutoff.timestamp() * 1000
                ):
                    return False
                if not row.parent_workflow_id:
                    return True
                row = known.get(row.parent_workflow_id)
            return False

        eligible = []
        for row in rows:
            # Explicit empty filter includes all applications; None defaults to this client.
            if safe(row) and not await listing(
                parent_workflow_id=row.workflow_id, application_name=[], limit=1
            ):
                eligible.append(row.workflow_id)
        if apply and eligible:
            from functools import partial

            await blocking(partial(client.delete_workflows, eligible, delete_children=False))
        return {
            "examined": len(rows),
            "eligible": len(eligible),
            "deleted": len(eligible) if apply else 0,
            "next_offset": offset + len(rows) - (len(eligible) if apply else 0)
            if len(rows) == limit
            else 0,
        }
    finally:
        try:
            await blocking(client.destroy)
        finally:
            await blocking(engine.dispose)

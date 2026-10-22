"""Checkpoint discovery separately from durable child enqueue and bounded billing I/O."""

from dbos import DBOS

from keystone.core.errors import ServiceError
from keystone.core.execution.contracts import NoInput, TaskBinding
from keystone.core.execution.dispatch import enqueue_task
from keystone.core.execution.schedules import register_schedule
from keystone.core.execution.steps import safe_step
from keystone.platform.model_access.services.costs import CostService
from keystone.platform.model_access.tasks.definitions import (
    COST_SCAN,
    COST_SCHEDULE,
    COST_SCOPE,
    CostScopeInput,
)

QUEUE, QUERY_QUEUE = COST_SCAN.queue, COST_SCOPE.queue
WORKFLOW, QUERY_WORKFLOW, SCHEDULE = COST_SCAN.entrypoint, COST_SCOPE.entrypoint, COST_SCHEDULE.name


def register(runtime, health):
    service = CostService(runtime.database, runtime.models)
    health.require_task("model_costs")

    @safe_step(name="model_access.find_due_scopes", error_code="cost_scan_failed")
    async def find_due_scopes():
        return await service.scopes()

    @safe_step(name="model_access.reconcile_scope", error_code="cost_reconcile_failed")
    async def reconcile_scope(scope):
        try:
            await service.reconcile(scope)
        except ServiceError as error:
            if error.code not in {"model_refresh_busy", "model_capacity_busy"}:
                raise

    @DBOS.workflow(name=QUERY_WORKFLOW)
    async def cost_scope(payload):
        try:
            value = CostScopeInput.model_validate(payload)
            await reconcile_scope(value.scope)
        except Exception:
            raise RuntimeError("cost_reconcile_failed") from None

    scope_task = TaskBinding(COST_SCOPE, cost_scope)

    @DBOS.workflow(name=WORKFLOW)
    async def scan(payload):
        try:
            NoInput.model_validate(payload)
            for scope in await find_due_scopes():
                await enqueue_task(
                    scope_task,
                    {"scope": scope},
                    execution_key=f"cost-query:{DBOS.workflow_id}:{scope}",
                    coalesce_key=f"cost:{scope}",
                )
            health.checked_task("model_costs", True)
        except Exception:
            health.checked_task("model_costs", False)
            raise RuntimeError("cost_scan_failed") from None

    scan_task = TaskBinding(COST_SCAN, scan, monitor_key="model_costs")
    return (scan_task, scope_task), register_schedule(
        COST_SCHEDULE,
        scan_task,
        execution_prefix="cost-scan",
    )

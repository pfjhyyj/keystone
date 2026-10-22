"""Worker-only cost discovery and isolated bounded billing I/O."""

import logging
from datetime import timedelta

from dbos import DBOS

from keystone.core.dbos_runtime import configure_schedule
from keystone.core.time import utcnow
from keystone.platform.model_access.services.costs import CostService

log = logging.getLogger(__name__)
QUEUE = "keystone-model-maintenance"
QUERY_QUEUE = "keystone-system"
SCHEDULE = "keystone.model_access.reconcile_costs"
WORKFLOW = "keystone.model_access.cost_scan"
QUERY_WORKFLOW = "keystone.model_access.cost_scope"


def register(runtime, health):
    service = CostService(runtime.database, runtime.models)
    health.require_task("model_costs")

    @DBOS.workflow(name=QUERY_WORKFLOW)
    async def cost_scope(scope):
        try:
            await service.reconcile(scope)
        except Exception as error:
            from keystone.core.errors import ServiceError

            if isinstance(error, ServiceError) and error.code in {
                "model_refresh_busy",
                "model_capacity_busy",
            }:
                return
            # SQL/HTTP exceptions must never persist a credential URL in DBOS history.
            log.error("cost_reconcile_failed type=%s", type(error).__name__)
            raise RuntimeError("cost_reconcile_failed") from None

    @DBOS.workflow(name=WORKFLOW)
    async def scan():
        try:
            for scope in await service.scopes():
                await DBOS.enqueue_workflow_with_options_async(
                    {
                        "workflow_name": QUERY_WORKFLOW,
                        "queue_name": QUERY_QUEUE,
                        "workflow_id": f"cost-query:{DBOS.workflow_id}:{scope}",
                        "app_version": DBOS.application_version,
                        "deduplication_id": f"cost:{scope}:{DBOS.application_version}",
                        "duplication_policy": "return-existing",
                    },
                    scope,
                )
            health.checked_task("model_costs", True)
        except Exception as error:
            health.checked_task("model_costs", False)
            log.error("cost_scan_failed type=%s", type(error).__name__)
            raise RuntimeError("cost_scan_failed") from None

    @DBOS.workflow(name="keystone.model_access.cost_tick")
    async def tick(scheduled_at, context):
        if utcnow() - scheduled_at > timedelta(seconds=10):
            return
        await DBOS.enqueue_workflow_with_options_async(
            {
                "workflow_name": WORKFLOW,
                "queue_name": QUEUE,
                "workflow_id": f"cost-scan:{DBOS.workflow_id}",
                "app_version": DBOS.application_version,
                "deduplication_id": f"{SCHEDULE}:{DBOS.application_version}",
                "duplication_policy": "return-existing",
            }
        )

    async def install():
        await configure_schedule(name=SCHEDULE, workflow=tick, enabled=True)
        await tick(utcnow(), None)

    return install

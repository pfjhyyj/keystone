"""Periodic Flow reconciliation, separate from file I/O and business capacity."""

import logging
from datetime import timedelta

from dbos import DBOS

from keystone.core.dbos_runtime import configure_schedule
from keystone.core.logging import safe_exception
from keystone.core.time import utcnow
from keystone.platform.flow.reconcile import reconcile

log = logging.getLogger(__name__)
QUEUE = "keystone-flow-maintenance"
SCHEDULE = "keystone.flow.reconcile"
WORKFLOW = "keystone.flow.reconcile_pass"


def register(runtime, health):
    enabled = runtime.settings.flow_enabled
    if enabled:
        health.require_task("flow")

    @DBOS.workflow(name=WORKFLOW)
    async def reconcile_pass():
        if not enabled:
            return
        try:
            await reconcile(runtime, runtime.flow_engine)
            health.checked_task("flow", True)
        except Exception as error:
            health.checked_task("flow", False)
            log.error(
                "flow_reconcile_failed type=%s",
                type(error).__name__,
                extra={"exception": safe_exception(error)},
            )
            raise RuntimeError("flow_reconcile_failed") from None

    @DBOS.workflow(name="keystone.flow.reconcile_tick")
    async def tick(scheduled_at, context):
        if not enabled or utcnow() - scheduled_at > timedelta(seconds=10):
            return
        await DBOS.enqueue_workflow_with_options_async(
            {
                "workflow_name": WORKFLOW,
                "queue_name": QUEUE,
                "workflow_id": f"flow-maintenance:{DBOS.workflow_id}",
                "app_version": DBOS.application_version,
                "deduplication_id": f"{SCHEDULE}:{DBOS.application_version}",
                "duplication_policy": "return-existing",
            }
        )

    async def install():
        await configure_schedule(name=SCHEDULE, workflow=tick, enabled=enabled)
        if enabled:
            await tick(utcnow(), None)

    return install

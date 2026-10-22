"""Worker-only file maintenance; runtime handles stay out of DBOS inputs."""

import logging
from datetime import timedelta

from dbos import DBOS

from keystone.core.dbos_runtime import configure_schedule
from keystone.core.logging import safe_exception
from keystone.core.time import utcnow
from keystone.platform.files.services.cleanup import cleanup

log = logging.getLogger(__name__)
QUEUE = "keystone-files-maintenance"
SCHEDULE = "keystone.files.cleanup"
WORKFLOW = "keystone.files.cleanup_pass"


def register(runtime, health):
    enabled = bool(runtime.settings.file_storages)
    if enabled:
        health.require_task("files")

    @DBOS.workflow(name=WORKFLOW)
    async def cleanup_pass():
        if not enabled:
            return
        try:
            state = await runtime.readiness()
            database_ready = all(state["checks"].get(key) == "ok" for key in ("database", "schema"))
            if state["status"] == "ready" or database_ready:
                await cleanup(runtime.database, runtime.storage)
            health.checked_task("files", state["status"] == "ready")
        except Exception as error:
            health.checked_task("files", False)
            log.error(
                "file_cleanup_failed type=%s",
                type(error).__name__,
                extra={"exception": safe_exception(error)},
            )
            raise RuntimeError("file_cleanup_failed") from None

    @DBOS.workflow(name="keystone.files.cleanup_tick")
    async def tick(scheduled_at, context):
        if not enabled or utcnow() - scheduled_at > timedelta(seconds=10):
            return
        # A tick never waits for its child. Pending/running work holds the dedup key.
        await DBOS.enqueue_workflow_with_options_async(
            {
                "workflow_name": WORKFLOW,
                "queue_name": QUEUE,
                "workflow_id": f"files-maintenance:{DBOS.workflow_id}",
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

"""File I/O checkpoints retain the domain's leases, references and deletion rules."""

from dbos import DBOS

from keystone.core.execution.contracts import NoInput, TaskBinding
from keystone.core.execution.schedules import register_schedule
from keystone.core.execution.steps import safe_step
from keystone.platform.files.services.cleanup import cleanup
from keystone.platform.files.tasks.definitions import CLEANUP, CLEANUP_SCHEDULE

QUEUE, WORKFLOW, SCHEDULE = CLEANUP.queue, CLEANUP.entrypoint, CLEANUP_SCHEDULE.name


def register(runtime, health):
    enabled = bool(runtime.settings.file_storages)
    if enabled:
        health.require_task("files")

    @safe_step(name="files.cleanup", error_code="file_cleanup_failed")
    async def cleanup_step():
        state = await runtime.readiness()
        database_ready = all(state["checks"].get(key) == "ok" for key in ("database", "schema"))
        if state["status"] == "ready" or database_ready:
            await cleanup(runtime.database, runtime.storage)
        return state["status"] == "ready"

    @DBOS.workflow(name=WORKFLOW)
    async def cleanup_pass(payload):
        if not enabled:
            return
        try:
            NoInput.model_validate(payload)
            health.checked_task("files", await cleanup_step())
        except Exception:
            health.checked_task("files", False)
            raise RuntimeError("file_cleanup_failed") from None

    task = TaskBinding(CLEANUP, cleanup_pass, enabled, "files")
    return (task,), register_schedule(
        CLEANUP_SCHEDULE,
        task,
        execution_prefix="files-maintenance",
    )

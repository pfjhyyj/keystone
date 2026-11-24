"""Idle sessions are stopped by the Worker; provider calls stay inside one checkpointed step."""

from dbos import DBOS

from keystone.core.execution.contracts import NoInput, TaskBinding
from keystone.core.execution.schedules import register_schedule
from keystone.core.execution.steps import safe_step
from keystone.platform.environments.services.idle import stop_idle
from keystone.platform.environments.tasks.definitions import IDLE_STOP, IDLE_STOP_SCHEDULE

QUEUE, WORKFLOW, SCHEDULE = IDLE_STOP.queue, IDLE_STOP.entrypoint, IDLE_STOP_SCHEDULE.name


def register(runtime, health):
    enabled = runtime.opensandbox is not None
    if enabled:
        health.require_task("environments")

    @safe_step(name="environments.idle_stop", error_code="environment_idle_stop_failed")
    async def idle_step():
        await stop_idle(runtime.database, runtime.opensandbox, runtime.environment_keys)
        return True

    @DBOS.workflow(name=WORKFLOW)
    async def idle_stop_pass(payload):
        if not enabled:
            return
        try:
            NoInput.model_validate(payload)
            health.checked_task("environments", await idle_step())
        except Exception:
            health.checked_task("environments", False)
            raise RuntimeError("environment_idle_stop_failed") from None

    task = TaskBinding(IDLE_STOP, idle_stop_pass, enabled, "environments")
    return (task,), register_schedule(
        IDLE_STOP_SCHEDULE,
        task,
        execution_prefix="environments-maintenance",
    )

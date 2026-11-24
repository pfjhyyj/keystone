"""Deliveries are sent by the Worker; each pass claims, sends and records one batch."""

from dbos import DBOS

from keystone.core.execution.contracts import NoInput, TaskBinding
from keystone.core.execution.schedules import register_schedule
from keystone.core.execution.steps import safe_step
from keystone.platform.messaging.services import inbound
from keystone.platform.messaging.services.dispatch import dispatch
from keystone.platform.messaging.tasks.definitions import DISPATCH, DISPATCH_SCHEDULE

QUEUE, WORKFLOW, SCHEDULE = DISPATCH.queue, DISPATCH.entrypoint, DISPATCH_SCHEDULE.name


def register(runtime, health):
    health.require_task("messaging")

    @safe_step(name="messaging.dispatch", error_code="message_dispatch_failed")
    async def dispatch_step():
        vault = runtime.messaging_vault()
        # Inbound first, so replies it queues can go out in the same pass.
        await inbound.retry(runtime, vault)
        await dispatch(runtime, vault)
        return True

    @DBOS.workflow(name=WORKFLOW)
    async def dispatch_pass(payload):
        try:
            NoInput.model_validate(payload)
            health.checked_task("messaging", await dispatch_step())
        except Exception:
            health.checked_task("messaging", False)
            raise RuntimeError("message_dispatch_failed") from None

    task = TaskBinding(DISPATCH, dispatch_pass, True, "messaging")
    return (task,), register_schedule(
        DISPATCH_SCHEDULE,
        task,
        execution_prefix="messaging-dispatch",
    )

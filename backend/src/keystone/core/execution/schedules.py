"""Code-owned schedules with guarded, checkpointed trigger acceptance."""

import asyncio

from dbos import DBOS

from keystone.core.execution.contracts import ScheduleDefinition, TaskBinding
from keystone.core.execution.dispatch import enqueue_task, task_payload
from keystone.core.execution.steps import safe_step
from keystone.core.time import utcnow


def register_schedule(plan: ScheduleDefinition, task: TaskBinding, *, execution_prefix: str):
    if plan.task_key != task.definition.key:
        raise ValueError("Schedule task does not match binding")
    payload = task_payload(task.definition, plan.payload)

    @safe_step(name=f"{plan.tick_entrypoint}.accept", error_code="task_trigger_failed")
    async def accept(scheduled_at):
        return (
            plan.max_lateness_seconds is None
            or (utcnow() - scheduled_at).total_seconds() <= plan.max_lateness_seconds
        )

    @DBOS.workflow(name=plan.tick_entrypoint)
    async def tick(scheduled_at, context):
        # Guard on every replay too: disabling a capability must stop residual ticks.
        if not task.enabled or not await accept(scheduled_at):
            return
        await enqueue_task(
            task,
            payload,
            execution_key=f"{execution_prefix}:{DBOS.workflow_id}",
            coalesce_key=plan.name if plan.overlap == "coalesce" else None,
        )

    async def install():
        if task.enabled:
            await DBOS.apply_schedules_async(
                [
                    {
                        "schedule_name": plan.name,
                        "workflow_fn": tick,
                        "schedule": plan.cron,
                        "cron_timezone": plan.timezone,
                        "automatic_backfill": plan.backfill,
                    }
                ]
            )
            # Updating code-owned configuration preserves PAUSED in DBOS.
            await asyncio.to_thread(DBOS.resume_schedule, plan.name)
            if plan.run_on_startup:
                await tick(utcnow(), None)
        elif await DBOS.get_schedule_async(plan.name) is not None:
            await asyncio.to_thread(DBOS.pause_schedule, plan.name)

    return install

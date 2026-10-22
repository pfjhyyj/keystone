"""Checkpoint claims/projections; keep idempotent engine effects outside steps."""

import asyncio
import logging

from dbos import DBOS

from keystone.core.errors import ServiceError
from keystone.core.execution.contracts import NoInput, TaskBinding
from keystone.core.execution.schedules import register_schedule
from keystone.core.execution.steps import safe_step
from keystone.core.logging import safe_exception
from keystone.platform.flow.services import reconciliation as service
from keystone.platform.flow.tasks.definitions import RECONCILE, RECONCILE_SCHEDULE

log = logging.getLogger(__name__)
QUEUE, WORKFLOW, SCHEDULE = RECONCILE.queue, RECONCILE.entrypoint, RECONCILE_SCHEDULE.name
ENGINE_TERMINAL = {"SUCCESS", "ERROR", "CANCELLED", "MAX_RECOVERY_ATTEMPTS_EXCEEDED"}


async def reconcile_one(runtime, engine, identifier, *, prepare=None, project=None):
    async def observe_and_prepare(identifier):
        return await service.prepare(runtime, identifier, await engine.state(identifier))

    async def observe_and_project(identifier, *, submission_error=None):
        await service.project(
            runtime,
            identifier,
            await engine.state(identifier),
            submission_error=submission_error,
        )

    action = await (prepare or observe_and_prepare)(identifier)
    if action == "skip":
        return
    submission_error = None
    if action == "submit":
        # Deliberate live read on replay: never authorize from a cached plan, and
        # never fail an already-submitted successful run because its actor changed.
        if await engine.state(identifier) is None:
            try:
                candidate = await service.submission(runtime, identifier)
            except ServiceError as error:
                if error.code != "execution_not_authorized":
                    raise
                candidate, submission_error = None, error.code
            if candidate is not None:
                await engine.enqueue(*candidate)
    elif action == "cancel":
        state = await engine.state(identifier)
        if (
            state
            and state["status"] not in ENGINE_TERMINAL
            and await service.prepare(runtime, identifier, state) == "cancel"
        ):
            await engine.cancel(identifier)
    await (project or observe_and_project)(identifier, submission_error=submission_error)


def register(runtime, health):
    enabled = runtime.settings.flow_enabled
    if enabled:
        health.require_task("flow")

    @safe_step(name="flow.claim_batch", error_code="flow_reconcile_failed")
    async def claim_batch():
        return await service.claim_batch(runtime)

    @safe_step(name="flow.prepare", error_code="flow_sync_deferred")
    async def prepare(identifier):
        return await service.prepare(
            runtime, identifier, await runtime.flow_engine.state(identifier)
        )

    @safe_step(name="flow.project", error_code="flow_sync_deferred")
    async def project(identifier, *, submission_error=None):
        await service.project(
            runtime,
            identifier,
            await runtime.flow_engine.state(identifier),
            submission_error=submission_error,
        )

    @DBOS.workflow(name="keystone.flow.sync_run")
    async def sync_run(identifier):
        # The child result checkpoints the per-run timeout/failure. Otherwise a
        # timeout followed by another run's steps can change replay ordering.
        try:
            async with asyncio.timeout(15):
                await reconcile_one(
                    runtime,
                    runtime.flow_engine,
                    identifier,
                    prepare=prepare,
                    project=project,
                )
        except Exception as error:
            log.warning(
                "flow_sync_deferred run_id=%s type=%s",
                identifier,
                type(error).__name__,
                extra={"exception": safe_exception(error)},
            )
            raise RuntimeError("flow_sync_deferred") from None

    @DBOS.workflow(name=WORKFLOW)
    async def reconcile_pass(payload):
        if not enabled:
            return
        try:
            NoInput.model_validate(payload)
            for identifier in await claim_batch():
                try:
                    await sync_run(identifier)
                except Exception:
                    pass  # This run remains a domain pending fact for a later pass.
            health.checked_task("flow", True)
        except Exception:
            health.checked_task("flow", False)
            raise RuntimeError("flow_reconcile_failed") from None

    task = TaskBinding(RECONCILE, reconcile_pass, enabled, "flow")
    return (task,), register_schedule(
        RECONCILE_SCHEDULE,
        task,
        execution_prefix="flow-maintenance",
    )

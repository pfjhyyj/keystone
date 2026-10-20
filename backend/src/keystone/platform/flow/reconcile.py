import asyncio
import logging

from jsonschema import Draft202012Validator, FormatChecker
from sqlalchemy import select

from keystone.core.errors import ServiceError
from keystone.core.logging import safe_exception
from keystone.core.time import utcnow
from keystone.platform.audit.services.writer import Actor, record
from keystone.platform.flow.catalog import execution_version, small_json
from keystone.platform.flow.files import retain_outputs
from keystone.platform.flow.identity import ExecutionIdentity
from keystone.platform.flow.models import TERMINAL, FlowRun

log = logging.getLogger(__name__)


async def finish(session, run, status, result=None, error=None):
    if run.status in TERMINAL:
        return
    if status == "succeeded":
        try:
            small_json(result)
            Draft202012Validator(
                run.definition_snapshot["output_schema"], format_checker=FormatChecker()
            ).validate(result)
            # Savepoint prevents partial references surviving output validation failure.
            async with session.begin_nested():
                await retain_outputs(session, run, result)
        except (ServiceError, ValueError, KeyError, TypeError) as exc:
            raise ServiceError(409, "flow_invalid_output", "流程输出不符合原契约") from exc
    run.status, run.result, run.error_code = (
        status,
        result if status == "succeeded" else None,
        error,
    )
    run.finished_at = utcnow()
    await record(
        session,
        "flow.complete",
        Actor("system"),
        "flow.run",
        run.id,
        details={"status": status, "error_code": error},
    )


async def reconcile_one(runtime, engine, identifier):
    state = await engine.state(identifier)
    async with runtime.database.transaction() as session:
        run = await session.get(FlowRun, identifier, with_for_update=True)
        if run is None or run.status in TERMINAL:
            return
        run.observed_at = utcnow()
        definition = runtime.flow_definitions.get(run.definition_key)
        matching = definition and run.execution_version == execution_version()
        cancel = run.cancel_requested_at is not None
        if state and (
            state["version"] != run.execution_version
            or (matching and state["name"] != definition.entrypoint)
        ):
            log.error("flow_engine_version_mismatch run_id=%s", identifier)
            return
    if state is None and not cancel:
        if not matching:
            log.warning("flow_version_unavailable run_id=%s", identifier)
            return
        try:
            async with ExecutionIdentity(runtime, identifier).access():
                pass
        except ServiceError as error:
            if error.code == "flow_cancelled":
                return
            if error.code != "execution_not_authorized":
                raise
            async with runtime.database.transaction() as session:
                current = await session.get(FlowRun, identifier, with_for_update=True)
                await finish(session, current, "failed", error=error.code)
            return
        await engine.enqueue(run, definition)
        state = await engine.state(identifier)
    if (
        cancel
        and state
        and state["status"]
        not in {"SUCCESS", "ERROR", "CANCELLED", "MAX_RECOVERY_ATTEMPTS_EXCEEDED"}
    ):
        await engine.cancel(identifier)
        state = await engine.state(identifier)
    async with runtime.database.transaction() as session:
        current = await session.get(FlowRun, identifier, with_for_update=True)
        if current.status in TERMINAL:
            return
        current.observed_at = utcnow()
        if state is None:
            if current.cancel_requested_at:
                await finish(session, current, "cancelled")
            return
        status = state["status"]
        if status == "SUCCESS":
            from jsonschema import ValidationError

            try:
                await finish(session, current, "succeeded", result=state["result"])
            except (ServiceError, ValidationError):
                await finish(session, current, "failed", error="flow_invalid_output")
        elif status in {"ERROR", "MAX_RECOVERY_ATTEMPTS_EXCEEDED"}:
            if current.cancel_requested_at and state["error_code"] == "flow_cancelled":
                await finish(session, current, "cancelled")
            else:
                await finish(session, current, "failed", error=state["error_code"])
        elif status == "CANCELLED":
            await finish(session, current, "cancelled")
        elif status == "PENDING":
            current.status = "running"


async def reconcile(runtime, engine, cursor=None):
    # Keyset cursor prevents old-version runs starving later rows, without extra fields.
    async with runtime.database.transaction() as session:
        query = select(FlowRun.id).where(FlowRun.status.not_in(TERMINAL))
        if cursor:
            query = query.where(FlowRun.id > cursor)
        ids = list(await session.scalars(query.order_by(FlowRun.id).limit(25)))
    for identifier in ids:
        try:
            async with asyncio.timeout(15):
                await reconcile_one(runtime, engine, identifier)
        except Exception as error:
            log.warning(
                "flow_sync_deferred run_id=%s type=%s",
                identifier,
                type(error).__name__,
                extra={"exception": safe_exception(error)},
            )
    return ids[-1] if len(ids) == 25 else None

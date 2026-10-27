import logging

from jsonschema import Draft202012Validator, FormatChecker
from sqlalchemy import func, select

from keystone.core.errors import ServiceError
from keystone.core.time import utcnow
from keystone.platform.audit.services.writer import Actor, record
from keystone.platform.flow.models import TERMINAL, FlowRun
from keystone.platform.flow.schemas.snapshots import matches_definition, read_snapshot
from keystone.platform.flow.schemas.values import small_json
from keystone.platform.flow.services.identity import ExecutionIdentity
from keystone.platform.flow.services.outputs import retain_outputs

log = logging.getLogger(__name__)


async def backlog(database):
    async with database.transaction() as session:
        pending, oldest = (
            await session.execute(
                select(func.count(), func.min(FlowRun.created_at)).where(
                    FlowRun.status == "pending"
                )
            )
        ).one()
        return pending, oldest


def matches_observation(runtime, run, state):
    definition = runtime.flow_definitions.get(run.definition_key)
    snapshot = read_snapshot(run.definition_snapshot)
    expected = snapshot.get("entrypoint")
    if expected is None and definition and run.execution_version == runtime.execution_version():
        expected = definition.entrypoint
    return not state or (
        state["version"] == run.execution_version
        and (expected is None or state["name"] == expected)
    )


async def claim_batch(runtime):
    # Fairness belongs to the domain, independent of the Worker receiving this pass.
    async with runtime.database.transaction() as session:
        rows = list(
            await session.scalars(
                select(FlowRun)
                .where(FlowRun.status.not_in(TERMINAL))
                .order_by(
                    func.coalesce(FlowRun.reconcile_attempted_at, FlowRun.created_at), FlowRun.id
                )
                .limit(25)
                .with_for_update(skip_locked=True)
            )
        )
        for row in rows:
            row.reconcile_attempted_at = utcnow()
        return [row.id for row in rows]


async def prepare(runtime, identifier, state):
    async with runtime.database.transaction() as session:
        run = await session.get(FlowRun, identifier, with_for_update=True)
        if run is None or run.status in TERMINAL:
            return "skip"
        run.observed_at = utcnow()
        if not matches_observation(runtime, run, state):
            log.error("flow_engine_version_mismatch run_id=%s", identifier)
            return "skip"
        if run.cancel_requested_at is not None:
            return "cancel"
        if state is None:
            if (
                run.definition_key not in runtime.flow_definitions
                or run.execution_version != runtime.execution_version()
            ):
                log.warning("flow_version_unavailable run_id=%s", identifier)
                return "skip"
            return "submit"
        return "project"


async def submission(runtime, identifier):
    """Live authorization before an idempotent effect, also on coordinator replay."""
    try:
        async with ExecutionIdentity(runtime, identifier).access():
            pass
    except ServiceError as error:
        if error.code == "flow_cancelled":
            return None
        # The engine may have advanced since the coordinator's first observation.
        # Let projection observe it again before deciding authorization prevented
        # submission; an already completed execution remains a business fact.
        raise
    async with runtime.database.transaction() as session:
        run = await session.get(FlowRun, identifier, with_for_update=True)
        if run is None or run.status in TERMINAL or run.cancel_requested_at:
            return None
        definition = runtime.flow_definitions.get(run.definition_key)
        if (
            definition is None
            or run.execution_version != runtime.execution_version()
            or not matches_definition(read_snapshot(run.definition_snapshot), definition)
        ):
            return None
        return run, definition


async def project(runtime, identifier, state, *, submission_error=None):
    async with runtime.database.transaction() as session:
        current = await session.get(FlowRun, identifier, with_for_update=True)
        if current is None or current.status in TERMINAL:
            return
        if not matches_observation(runtime, current, state):
            log.error("flow_engine_version_mismatch run_id=%s", identifier)
            return
        current.observed_at = utcnow()
        if state is None:
            if current.cancel_requested_at:
                await finish(session, current, "cancelled")
            elif submission_error == "execution_not_authorized":
                await finish(session, current, "failed", error=submission_error)
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


async def finish(session, run, status, result=None, error=None):
    if run.status in TERMINAL:
        return
    if status == "succeeded":
        try:
            small_json(result)
            snapshot = read_snapshot(run.definition_snapshot)
            Draft202012Validator(
                snapshot["output_schema"], format_checker=FormatChecker()
            ).validate(result)
            # Savepoint prevents partial references surviving output validation failure.
            async with session.begin_nested():
                await retain_outputs(session, run, result)
        except (ServiceError, ValueError, KeyError, TypeError) as exc:
            raise ServiceError(409, "flow_invalid_output", "流程输出不符合原契约") from exc
    if status != "succeeded":
        from keystone.platform.model_access.services.image_tasks import abandon_in_transaction

        await abandon_in_transaction(session, run.id)
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

"""Record code-owned projections only. Step checkpoints remain private."""

import logging
from contextvars import ContextVar
from functools import wraps

from dbos import DBOS

from keystone.core.errors import ServiceError
from keystone.platform.flow.schemas.errors import ERRORS
from keystone.platform.flow.schemas.step_diagnostics import (
    MAX_ATTACHMENTS,
    StepAttachment,
    StepAttempt,
    StepFailure,
    attempt_prefix,
)
from keystone.platform.flow.schemas.values import small_json
from keystone.platform.flow.workflows.context import flow_context

_current = ContextVar("flow_step_diagnostic", default=None)
log = logging.getLogger(__name__)


def diagnostic_input(**values):
    """Declare safe JSON fields from inside the executing Step."""
    state = _current.get()
    if state is not None:
        state["input"].update(values)


def diagnostic_attachment(label, file_id):
    """Opt an existing Run file into one attempt's diagnostic preview."""
    state = _current.get()
    if state is not None and len(state["attachments"]) < MAX_ATTACHMENTS:
        state["attachments"].append({"label": label, "file_id": file_id})


def _failure(error, safe_codes):
    if isinstance(error, ServiceError) and error.code in safe_codes:
        return StepFailure(code=error.code, message=error.detail[:300])
    if isinstance(error, ServiceError) and error.code in ERRORS:
        return StepFailure(code=error.code, message="阶段执行失败，请根据错误码排查。")
    return StepFailure(code="flow_execution_failed", message="阶段执行时发生内部错误。")


async def _save(run_id, key, attempt, label, kind, state, output, error):
    try:
        detail = {
            "attempt": attempt,
            "label": label,
            "kind": kind,
            "input": state["input"] or None,
            "output": output,
            "error": error.model_dump() if error else None,
            "attachments": [
                StepAttachment.model_validate(item).model_dump(mode="json")
                for item in state["attachments"]
            ],
        }
        try:
            raw = small_json(detail)
        except ServiceError:
            # A large projection must not lose the failure or the approved attachments.
            limit_note = {"说明": "详情超过单次记录上限"}
            for field in ("input", "output"):
                if detail[field] is not None:
                    detail[field] = limit_note
            raw = small_json(detail)
        StepAttempt.model_validate_json(raw)
        await flow_context(run_id).upload(
            key, "step-diagnostic.json", raw, content_type="application/json"
        )
    except Exception as failure:
        # Observation must never change the outcome of the workflow.
        log.warning(
            "flow_step_diagnostic_unavailable run_id=%s type=%s", run_id, type(failure).__name__
        )


def diagnose_step(label, *, project=None, safe_codes=()):
    """Place under DBOS.step. The caller explicitly projects the returned value."""

    def decorate(step):
        @wraps(step)
        async def execute(run_id, *args, **kwargs):
            workflow_id, step_id = DBOS.workflow_id, DBOS.step_id
            if workflow_id is None or step_id is None:
                return await step(run_id, *args, **kwargs)
            status = DBOS.step_status
            attempt = (status.current_attempt or 0) + 1 if status is not None else 1
            key = attempt_prefix(workflow_id, step_id) + f"{attempt:010d}"
            kind = "correction" if kwargs.get("correct_format") else "original"
            state = {"input": {}, "attachments": []}
            token = _current.set(state)
            try:
                try:
                    result = await step(run_id, *args, **kwargs)
                except Exception as error:
                    await _save(
                        run_id, key, attempt, label, kind, state, None, _failure(error, safe_codes)
                    )
                    raise
                try:
                    output = project(result) if project is not None else None
                except Exception as failure:
                    log.warning(
                        "flow_step_diagnostic_projection_failed run_id=%s type=%s",
                        run_id,
                        type(failure).__name__,
                    )
                    output = None
                await _save(run_id, key, attempt, label, kind, state, output, None)
                return result
            finally:
                _current.reset(token)

        return execute

    return decorate

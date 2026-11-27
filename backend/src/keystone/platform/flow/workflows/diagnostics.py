"""Record code-owned projections and default model call diagnostics. Checkpoints stay private."""

import logging
from contextvars import ContextVar
from functools import wraps
from uuid import UUID

from dbos import DBOS

from keystone.core.errors import ServiceError
from keystone.core.time import utcnow
from keystone.platform.flow.schemas.errors import ERRORS
from keystone.platform.flow.schemas.step_diagnostics import (
    MAX_ATTACHMENTS,
    MAX_DETAIL_BYTES,
    MAX_MODEL_CALLS,
    StepAttachment,
    StepAttempt,
    StepFailure,
    StepModelCall,
    attempt_prefix,
)
from keystone.platform.flow.schemas.values import small_json
from keystone.platform.flow.workflows.context import _execution_scope, _resources, _runtime

_current = ContextVar("flow_step_diagnostic", default=None)
log = logging.getLogger(__name__)

OPERATION_LABELS = {
    "text.generate": "文本生成",
    "text.structured": "结构化输出",
    "text.embed": "向量生成",
    "image.understand": "图像理解",
    "image.generate": "图片生成",
    "image.edit": "图片编辑",
}


def diagnostic_enabled():
    """Whether this Step records diagnostics; skip building costly fields otherwise."""
    return _current.get() is not None


def diagnostic_input(**values):
    """Declare safe JSON fields from inside the executing Step."""
    state = _current.get()
    if state is not None:
        state["input"].update(values)


def _add_attachment(items, label, file_id, role="artifact", *, prefer_existing=False):
    normalized = str(UUID(str(file_id)))
    for existing in items:
        if str(existing["file_id"]) == normalized and existing.get("role", "artifact") == role:
            if not prefer_existing:
                existing["label"] = label[:80]
            return
    if len(items) < MAX_ATTACHMENTS:
        items.append({"label": label[:80], "file_id": normalized, "role": role})


def diagnostic_attachment(label, file_id, *, role="artifact"):
    """Opt an existing Run file into one attempt's diagnostic preview."""
    state = _current.get()
    if state is not None:
        _add_attachment(state["attachments"], label, file_id, role=role)


def _failure(error, safe_codes=()):
    if isinstance(error, ServiceError) and error.code in safe_codes:
        return StepFailure(code=error.code, message=error.detail[:300])
    if isinstance(error, ServiceError) and error.code in ERRORS:
        return StepFailure(code=error.code, message="阶段执行失败，请根据错误码排查。")
    return StepFailure(code="flow_execution_failed", message="阶段执行时发生内部错误。")


def _truncate_strings(value, max_chars):
    if isinstance(value, str):
        if len(value) <= max_chars:
            return value
        omitted = len(value) - max_chars
        return f"{value[:max_chars]}…[已截断，省略 {omitted} 字符]"
    if isinstance(value, list):
        return [_truncate_strings(item, max_chars) for item in value[:64]]
    if isinstance(value, dict):
        return {str(key): _truncate_strings(item, max_chars) for key, item in value.items()}
    return value


def _fit_json(detail, max_bytes=MAX_DETAIL_BYTES):
    def encode(candidate):
        raw = small_json(candidate)
        if len(raw) > max_bytes:
            raise ServiceError(422, "flow_value_too_large", "流程数据超过单次记录上限")
        return raw

    try:
        return encode(detail)
    except ServiceError:
        pass
    for limit in (8000, 3000, 1200, 400, 120):
        candidate = {
            **detail,
            "input": _truncate_strings(detail["input"], limit),
            "output": _truncate_strings(detail["output"], limit),
            "model_calls": [
                {
                    **call,
                    "input": _truncate_strings(call.get("input"), limit),
                    "output": _truncate_strings(call.get("output"), limit),
                }
                for call in detail.get("model_calls", ())
            ],
        }
        try:
            return encode(candidate)
        except ServiceError:
            continue
    limit_note = {"说明": "详情超过单次记录上限"}
    fallback = {**detail}
    for field in ("input", "output"):
        if fallback[field] is not None:
            fallback[field] = limit_note
    fallback["model_calls"] = [
        {
            **call,
            "input": limit_note if call.get("input") is not None else None,
            "output": limit_note if call.get("output") is not None else None,
        }
        for call in fallback.get("model_calls", ())
    ]
    return encode(fallback)


def _duration_ms(started_at, finished_at):
    if started_at is None or finished_at is None or finished_at < started_at:
        return None
    return round((finished_at - started_at).total_seconds() * 1000)


async def _save(run_id, key, attempt, label, kind, state, output, error):
    try:
        started_at = state.get("started_at")
        finished_at = state.get("finished_at")
        duration_ms = state.get("duration_ms")
        if duration_ms is None:
            duration_ms = _duration_ms(started_at, finished_at)
        detail = {
            "attempt": attempt,
            "label": label[:80],
            "kind": kind,
            "started_at": started_at.isoformat() if started_at else None,
            "finished_at": finished_at.isoformat() if finished_at else None,
            "duration_ms": duration_ms,
            "input": state["input"] or None,
            "output": output,
            "error": error.model_dump() if error else None,
            "attachments": [
                StepAttachment.model_validate(item).model_dump(mode="json")
                for item in state["attachments"][:MAX_ATTACHMENTS]
            ],
            "model_calls": [
                StepModelCall.model_validate(item).model_dump(mode="json")
                for item in state.get("model_calls", ())[:MAX_MODEL_CALLS]
            ],
        }
        max_bytes = min(MAX_DETAIL_BYTES, _runtime().settings.file_max_bytes)
        raw = _fit_json(detail, max_bytes=max_bytes)
        StepAttempt.model_validate_json(raw)
        async with _execution_scope(run_id):
            await _resources(run_id).upload(
                key, "step-diagnostic.json", raw, content_type="application/json"
            )
    except Exception as failure:
        # Observation must never change the outcome of the workflow.
        log.warning(
            "flow_step_diagnostic_unavailable run_id=%s type=%s code=%s",
            run_id,
            type(failure).__name__,
            failure.code if isinstance(failure, ServiceError) else None,
        )


def _model_label(operation, model_role=None, node_key=None):
    base = OPERATION_LABELS.get(operation, operation)
    suffix = node_key or model_role
    return f"{base} · {suffix}" if suffix else base


async def record_model_call(
    run_id,
    *,
    operation,
    model_role=None,
    node_key=None,
    invocation_id=None,
    input_data=None,
    output_data=None,
    error=None,
    started_at=None,
    finished_at=None,
    attachments=(),
    enabled=True,
):
    """Record a model call's bounded input/output into the current step diagnostic."""
    if not enabled:
        return
    state = _current.get()
    if state is not None and not state.get("capture_model", True):
        return
    failure = _failure(error) if error is not None else None
    duration = _duration_ms(started_at, finished_at)
    call = {
        "operation": operation,
        "model_role": model_role,
        "node_key": node_key,
        "invocation_id": str(UUID(str(invocation_id))) if invocation_id is not None else None,
        "duration_ms": duration,
        "input": input_data,
        "output": output_data,
        "error": failure.model_dump() if failure else None,
    }
    if state is not None:
        if len(state["model_calls"]) < MAX_MODEL_CALLS:
            state["model_calls"].append(call)
        for item in attachments:
            _add_attachment(
                state["attachments"],
                item["label"],
                item["file_id"],
                role=item.get("role", "artifact"),
                prefer_existing=True,
            )
        return
    workflow_id, step_id = DBOS.workflow_id, DBOS.step_id
    if workflow_id is None or step_id is None:
        return
    status = DBOS.step_status
    attempt = (status.current_attempt or 0) + 1 if status is not None else 1
    key = attempt_prefix(workflow_id, step_id) + f"{attempt:010d}"
    standalone = {
        "input": {},
        "attachments": [],
        "model_calls": [call],
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_ms": duration,
    }
    for item in attachments:
        _add_attachment(
            standalone["attachments"],
            item["label"],
            item["file_id"],
            role=item.get("role", "artifact"),
        )
    await _save(
        run_id,
        key,
        attempt,
        _model_label(operation, model_role, node_key),
        "original",
        standalone,
        None,
        failure,
    )


def diagnose_step(label, *, project=None, safe_codes=(), capture_model=True):
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
            state = {
                "input": {},
                "attachments": [],
                "model_calls": [],
                "capture_model": capture_model,
                "started_at": utcnow(),
                "finished_at": None,
            }
            token = _current.set(state)
            try:
                try:
                    result = await step(run_id, *args, **kwargs)
                except Exception as error:
                    state["finished_at"] = utcnow()
                    await _save(
                        run_id, key, attempt, label, kind, state, None, _failure(error, safe_codes)
                    )
                    raise
                state["finished_at"] = utcnow()
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

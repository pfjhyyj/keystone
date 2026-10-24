"""Safe diagnostics for failed sends, shared by API, Flow and Agent execution."""

import asyncio
import logging
from time import monotonic

from keystone.core.logging import safe_exception
from keystone.platform.model_access.adapters.inference.errors import (
    classify_error,
    network_reason,
    upstream_error,
)

log = logging.getLogger(__name__)


def log_failure(
    error,
    *,
    identifier,
    selected,
    provider,
    correlation_id,
    started,
    stage,
    registered=False,
    finished=False,
    observation=None,
    upstream_request_id=None,
    flow_context=None,
    worker_identity=None,
):
    http_status, code, _, outcome = classify_error(error)
    if stage in {"persist_receipt", "persist_result"}:
        http_status, code, outcome = 503, "model_result_unavailable", "unknown"
    diagnostic = {
        "invocation_id": str(identifier),
        "channel_id": str(selected.channel_id),
        "model_entry_id": str(selected.model_entry_id),
        "provider": provider,
        "provider_model": selected.provider_model,
        "operation": selected.operation,
        "delivery": selected.delivery,
        "stage": stage,
        "elapsed_ms": max(0, round((monotonic() - started) * 1000)),
        "registered": registered,
        "record_finished": finished,
        "http_status": http_status,
        "error_code": code,
        "outcome": outcome,
    }
    if observation is not None:
        diagnostic.update(
            transport_phase=observation.phase,
            request_bytes=observation.request_bytes,
            response_bytes=observation.total_bytes,
            upstream_status=observation.upstream_status,
            receipt_ids=dict(observation.receipt_ids),
        )
    diagnostic.update(upstream_error(error))
    if reason := network_reason(error):
        diagnostic["network_reason"] = reason
    if upstream_request_id:
        diagnostic["upstream_request_id"] = upstream_request_id
    if flow_context is not None:
        # Only stable identifiers, never node inputs, workflow arguments or checkpoints.
        diagnostic["flow_run_id"] = str(flow_context.flow_run_id)
        if flow_context.agent_call_id is not None:
            diagnostic["agent_call_id"] = str(flow_context.agent_call_id)
    if worker_identity is not None:
        diagnostic["worker_id"] = worker_identity.worker_id
        diagnostic["worker_instance_id"] = str(worker_identity.instance_id)
    interrupted = code == "model_interrupted" or isinstance(
        error, (asyncio.CancelledError, GeneratorExit)
    )
    log.log(
        logging.WARNING if interrupted else logging.ERROR,
        "model_call_interrupted" if interrupted else "model_call_failed",
        extra={
            "request_id": correlation_id,
            "diagnostic": diagnostic,
            "exception": safe_exception(error),
        },
    )

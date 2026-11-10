import asyncio
import json
from time import monotonic
from uuid import uuid4

import pytest

from keystone.core.logging import JsonFormatter
from keystone.platform.model_access.adapters.inference.transport import Observation
from keystone.platform.model_access.schemas.invocations import InvocationContext, Selection
from keystone.platform.model_access.services.diagnostics import log_failure
from keystone.worker.identity import WorkerIdentity


@pytest.mark.parametrize("stage", ["inference", "persist_receipt", "persist_result"])
def test_diagnostics_keep_flow_worker_ids_but_no_request_or_result_values(caplog, stage):
    selected = Selection(
        channel_id=uuid4(),
        model_entry_id=uuid4(),
        provider="openrouter",
        provider_model="chat-model",
        api_base="https://must-not-log.invalid?key=private-secret",
    )
    flow = InvocationContext(
        flow_run_id=uuid4(),
        agent_call_id=uuid4(),
        node_key="private-secret",
        workflow_id="private-secret",
        step_id=12,
    )
    worker = WorkerIdentity("worker-one", uuid4(), "version")
    observation = Observation(lambda _: None)
    observation.phase = "response_received"
    observation.upstream_status = 200
    observation.request_bytes, observation.total_bytes = 30, 20
    observation.buffer.extend(b"private-secret")
    observation.receipt_ids = {"body_id": "safe-id"}
    try:
        raise ValueError("private-secret")
    except ValueError as error:
        log_failure(
            error,
            identifier=uuid4(),
            selected=selected,
            provider=selected.provider,
            correlation_id="http-request-id",
            started=monotonic(),
            stage=stage,
            registered=True,
            observation=observation,
            flow_context=flow,
            worker_identity=worker,
        )
    record = caplog.records[-1]
    encoded = JsonFormatter().format(record)
    assert "private-secret" not in encoded
    assert record.diagnostic["flow_run_id"] == str(flow.flow_run_id)
    assert record.diagnostic["agent_call_id"] == str(flow.agent_call_id)
    assert record.diagnostic["worker_id"] == "worker-one"
    assert record.diagnostic["worker_instance_id"] == str(worker.instance_id)
    assert record.diagnostic["receipt_ids"] == {"body_id": "safe-id"}
    if stage.startswith("persist_"):
        assert record.diagnostic["http_status"] == 503
        assert record.diagnostic["error_code"] == "model_result_unavailable"
    assert json.loads(encoded)["exception"][0]["frames"][-1]["line"] > 0


def test_interruption_logs_without_changing_unknown_outcome(caplog):
    selected = Selection(
        channel_id=uuid4(),
        model_entry_id=uuid4(),
        provider="openrouter",
        provider_model="chat",
        api_base="https://openrouter.ai/api/v1",
    )
    log_failure(
        asyncio.CancelledError(),
        identifier=uuid4(),
        selected=selected,
        provider=selected.provider,
        correlation_id=None,
        started=monotonic(),
        stage="inference",
    )
    record = caplog.records[-1]
    assert record.msg == "model_call_interrupted" and record.levelname == "WARNING"
    assert record.diagnostic["error_code"] == "model_interrupted"
    assert record.diagnostic["outcome"] == "unknown"

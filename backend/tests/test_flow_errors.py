from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from keystone.platform.flow.engine import Engine


@pytest.mark.parametrize(
    "code",
    [
        "model_not_found",
        "model_target_changed",
        "model_input_too_large",
        "model_response_too_large",
        "model_stream_incomplete",
        "model_interrupted",
        "model_invalid_request",
        "upstream_model_not_found",
        "model_rate_limited",
        "execution_not_authorized",
    ],
)
async def test_flow_preserves_safe_execution_errors(code):
    engine = Engine.__new__(Engine)
    engine.client = SimpleNamespace(
        list_workflows_async=AsyncMock(
            return_value=[
                SimpleNamespace(
                    status="ERROR",
                    output=None,
                    error=RuntimeError(code),
                    app_version="test",
                    name="document_review",
                )
            ]
        )
    )
    assert (await engine.state(uuid4()))["error_code"] == code


@pytest.mark.parametrize("error", ["unknown_code", "model_invalid_request: secret body"])
async def test_flow_never_projects_unknown_codes_or_exception_messages(error):
    engine = Engine.__new__(Engine)
    engine.client = SimpleNamespace(
        list_workflows_async=AsyncMock(
            return_value=[
                SimpleNamespace(
                    status="ERROR",
                    output=None,
                    error=RuntimeError(error),
                    app_version="test",
                    name="document_review",
                )
            ]
        )
    )
    assert (await engine.state(uuid4()))["error_code"] == "flow_execution_failed"

import asyncio

import httpx2
import pytest
from openai import APIStatusError
from pydantic_ai.exceptions import ModelHTTPError

from keystone.core.errors import ServiceError
from keystone.platform.model_access.inference.errors import classify_error


@pytest.mark.parametrize(
    "status,code,state",
    [
        (401, "model_authentication_failed", "failed"),
        (402, "model_insufficient_credits", "failed"),
        (429, "model_rate_limited", "failed"),
        (500, "model_upstream_error", "unknown"),
    ],
)
def test_direct_and_agent_http_errors_have_the_same_safe_classification(status, code, state):
    raw = APIStatusError(
        "sensitive",
        response=httpx2.Response(
            status, request=httpx2.Request("POST", "https://example.invalid/chat/completions")
        ),
        body={"message": "sensitive"},
    )
    wrapped = ModelHTTPError(status_code=status, model_name="test", body="sensitive")
    wrapped.__cause__ = raw
    for error in (raw, wrapped, ModelHTTPError(status_code=status, model_name="test")):
        result = classify_error(error)
        assert result[0] == 502 and result[1] == code and result[3] == state
        assert "sensitive" not in result[2]


def test_wrapped_timeout_cancellation_and_platform_error_remain_unknown():
    for cause, code in (
        (httpx2.ReadTimeout("sensitive"), "model_timeout"),
        (asyncio.CancelledError(), "model_interrupted"),
        (ServiceError(503, "model_result_unavailable", "safe"), "model_result_unavailable"),
    ):
        wrapper = RuntimeError("sensitive")
        wrapper.__cause__ = cause
        result = classify_error(wrapper)
        assert result[1] == code and result[3] == "unknown" and "sensitive" not in result[2]
    # A malformed exception chain must not prevent recording the attempt's outcome.
    wrapper = RuntimeError("sensitive")
    wrapper.__cause__ = wrapper
    assert classify_error(wrapper)[1] == "model_response_unknown"

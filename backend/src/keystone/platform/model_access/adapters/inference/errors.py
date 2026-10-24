"""One safe SDK error classification, shared by direct and Agent calls."""

import asyncio

import httpx2
from openai import APIStatusError

from keystone.core.errors import ServiceError
from keystone.platform.model_access.schemas.errors import AdapterError


def classify_error(error):
    """One sanitized classification for public errors and persisted attempt status."""
    from pydantic_ai.exceptions import ModelHTTPError

    causes, seen = [], set()
    cause = error
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        causes.append(cause)
        cause = cause.__cause__
    # Preserve safe host/protocol errors through SDK wrappers; no raw upstream message escapes.
    for cause in causes:
        if isinstance(cause, (ServiceError, AdapterError)):
            return cause.status, cause.code, cause.detail, "unknown"
    for cause in causes:
        if isinstance(cause, (APIStatusError, ModelHTTPError)):
            code = {
                400: "model_invalid_request",
                401: "model_authentication_failed",
                402: "model_insufficient_credits",
                403: "model_access_denied",
                404: "upstream_model_not_found",
                422: "model_invalid_request",
                429: "model_rate_limited",
            }.get(cause.status_code, "model_upstream_error")
            status = "failed" if 400 <= cause.status_code < 500 else "unknown"
            return 502, code, "上游模型请求失败", status
        if (
            isinstance(cause, (TimeoutError, httpx2.TimeoutException))
            or type(cause).__name__ == "APITimeoutError"
        ):
            return 504, "model_timeout", "模型调用超时，结果与费用可能未知", "unknown"
        if isinstance(cause, (asyncio.CancelledError, GeneratorExit)):
            return 499, "model_interrupted", "模型调用已中断，结果与费用可能未知", "unknown"
    return 502, "model_response_unknown", "模型结果无法确认，不会自动重试", "unknown"

"""One safe SDK error classification, shared by direct and Agent calls."""

import asyncio
import socket
import ssl

import httpx2
from openai import APIConnectionError, APIStatusError, APITimeoutError

from keystone.core.errors import ServiceError
from keystone.core.logging import exception_chain
from keystone.platform.model_access.schemas.errors import AdapterError


def upstream_error(error):
    """Known protocol codes only; arbitrary error messages/body fields may contain secrets."""
    from pydantic_ai.exceptions import ModelHTTPError

    for cause in exception_chain(error):
        if not isinstance(cause, (APIStatusError, ModelHTTPError)):
            continue
        result = {"upstream_status": cause.status_code}
        body = cause.body
        if isinstance(body, dict):
            body = body.get("error", body)
        if isinstance(body, dict):
            for field in ("code", "type"):
                value = body.get(field)
                if isinstance(value, str) and value in {
                    "context_length_exceeded",
                    "max_tokens_exceeded",
                    "invalid_api_key",
                    "insufficient_quota",
                    "insufficient_credits",
                    "model_not_found",
                    "rate_limit_exceeded",
                    "tokens_limit_exceeded",
                    "invalid_request_error",
                    "authentication_error",
                    "permission_error",
                    "rate_limit_error",
                    "server_error",
                    "overloaded_error",
                    "content_policy_violation",
                    "content_filter",
                    "unsupported_parameter",
                }:
                    result["upstream_error_" + field] = value
        return result
    return {}


def network_reason(error):
    causes = list(exception_chain(error))
    # Prefer a concrete transport cause to a generic SDK connection wrapper.
    for kind, reason in (
        (httpx2.ConnectTimeout, "connect_timeout"),
        (httpx2.ReadTimeout, "read_timeout"),
        (httpx2.WriteTimeout, "write_timeout"),
        (httpx2.PoolTimeout, "pool_timeout"),
        (socket.gaierror, "dns_error"),
        (ssl.SSLError, "tls_error"),
        (ConnectionRefusedError, "connection_refused"),
        (ConnectionResetError, "connection_reset"),
        (httpx2.ConnectError, "connect_error"),
        (httpx2.ReadError, "read_error"),
        (httpx2.WriteError, "write_error"),
        (httpx2.ProtocolError, "protocol_error"),
        (TimeoutError, "deadline_exceeded"),
        (httpx2.TimeoutException, "timeout"),
        (APITimeoutError, "timeout"),
        ((APIConnectionError, httpx2.NetworkError, ConnectionError), "connection_error"),
    ):
        if any(isinstance(cause, kind) for cause in causes):
            return reason
    return None


def classify_error(error):
    """One sanitized classification for public errors and persisted attempt status."""
    from pydantic_ai.exceptions import ModelHTTPError

    causes = list(exception_chain(error))
    # Preserve safe host/protocol errors through SDK wrappers; no raw upstream message escapes.
    for cause in causes:
        if isinstance(cause, (ServiceError, AdapterError)):
            return cause.status, cause.code, cause.detail, "unknown"
    for cause in causes:
        if isinstance(cause, (APIStatusError, ModelHTTPError)):
            if (
                cause.status_code in {400, 413, 422}
                and upstream_error(cause).get("upstream_error_code") == "context_length_exceeded"
            ):
                return (
                    502,
                    "model_context_length_exceeded",
                    "输入与输出长度超过上游模型上下文上限",
                    "failed",
                )
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
            detail = (
                "上游未找到所选模型，请从渠道模型列表选择，并检查 Key 权限"
                if cause.status_code == 404
                else "上游模型请求失败"
            )
            return 502, code, detail, status
        if isinstance(cause, (TimeoutError, httpx2.TimeoutException, APITimeoutError)):
            return 504, "model_timeout", "模型调用超时，结果与费用可能未知", "unknown"
        if isinstance(cause, (asyncio.CancelledError, GeneratorExit)):
            return 499, "model_interrupted", "模型调用已中断，结果与费用可能未知", "unknown"
    if network_reason(error):
        return 502, "model_connection_error", "模型连接异常，结果与费用可能未知", "unknown"
    return 502, "model_response_unknown", "模型结果无法确认，不会自动重试", "unknown"

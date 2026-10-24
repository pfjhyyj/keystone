"""Bounded protocol observation. Billing meaning is assigned by the caller."""

import json
from decimal import Decimal

import httpx2

from keystone.core.errors import ServiceError
from keystone.platform.model_access.adapters.inference.limits import retry_after
from keystone.platform.model_access.adapters.receipts import request_id


def usage(data):
    if not isinstance(data, dict):
        return None
    result = {}
    fields = {
        "prompt_tokens": "input_tokens",
        "completion_tokens": "output_tokens",
        "total_tokens": "total_tokens",
        "prompt_cache_hit_tokens": "cache_read_input_tokens",
        "prompt_cache_miss_tokens": "cache_miss_input_tokens",
    }
    for source, target in fields.items():
        value = data.get(source)
        if type(value) is int and 0 <= value < 2**63:
            result[target] = value
    for source, detail, target in (
        ("prompt_tokens_details", "cached_tokens", "cache_read_input_tokens"),
        ("completion_tokens_details", "reasoning_tokens", "reasoning_tokens"),
    ):
        extra = data.get(source)
        value = extra.get(detail) if isinstance(extra, dict) else None
        if type(value) is int and 0 <= value < 2**63:
            result[target] = value
    return result or None


class Observation:
    def __init__(self, on_update, *, max_bytes=4 * 1024 * 1024):
        self.max_bytes = max_bytes
        self.on_update = on_update
        self.streaming = self.done = False
        self.buffer = bytearray()
        self.total_bytes = 0
        self.receipt_ids = {}
        self.usage = None
        self.reported_cost = None
        self.rate_limited = False
        self.retry_after_seconds = None
        self.request_budget = None

    def parse(self, raw):
        if raw == b"[DONE]":
            self.done = True
            return
        try:
            payload = json.loads(raw, parse_float=Decimal)
            if not isinstance(payload, dict):
                return
            identifier = request_id(payload.get("id"))
            if identifier:
                self.receipt_ids["body_id"] = identifier
            data = payload.get("usage")
            if isinstance(data, dict):
                self.usage = usage(data)
                self.reported_cost = data.get("cost")
            self.on_update(self)
            # OpenRouter can report an upstream error inside an HTTP 200 SSE response.
            # Preserve receipts above, but never expose its raw error text or mark success.
            choices = payload.get("choices")
            if "error" in payload or (
                isinstance(choices, list)
                and any(isinstance(c, dict) and c.get("finish_reason") == "error" for c in choices)
            ):
                raise ServiceError(502, "model_upstream_error", "上游返回错误，结果与费用可能未知")
        except (ValueError, TypeError, RecursionError):
            pass

    def feed(self, chunk):
        self.total_bytes += len(chunk)
        if self.total_bytes > self.max_bytes:
            raise ServiceError(502, "model_response_too_large", "模型响应超过上限")
        self.buffer.extend(chunk)
        if self.streaming:
            while b"\n" in self.buffer:
                line, _, rest = self.buffer.partition(b"\n")
                self.buffer = bytearray(rest)
                if len(line) > 65536:
                    raise ServiceError(502, "model_response_too_large", "模型响应超过上限")
                if line.startswith(b"data:"):
                    self.parse(line[5:].strip())
            if len(self.buffer) > 65536:
                raise ServiceError(502, "model_response_too_large", "模型响应超过上限")


class ObservedResponse(httpx2.Response):
    def __init__(self, response, request, observation):
        self.observation = observation
        super().__init__(
            response.status_code,
            headers=response.headers,
            extensions=response.extensions,
            stream=response.stream,
            request=request,
        )

    async def aiter_bytes(self, chunk_size=None):
        async for chunk in super().aiter_bytes(chunk_size):
            self.observation.feed(chunk)
            yield chunk
        if not self.observation.streaming and self.status_code < 400:
            self.observation.parse(bytes(self.observation.buffer))
        self.observation.buffer.clear()


class SendTransport(httpx2.AsyncBaseTransport):
    def __init__(
        self, source, before_send, observation, on_receipt, validate, identifier, *, authorize
    ):
        self.source, self.before_send, self.observation = source, before_send, observation
        self.on_receipt, self.validate, self.identifier = on_receipt, validate, identifier
        self.authorize = authorize

    async def handle_async_request(self, request):
        self.observation.request_budget = self.validate(request)
        secret = await self.before_send()
        # Authorization and committed registration precede the only physical send.
        request.headers.pop("Authorization", None)
        self.authorize(request, secret)
        request.headers["X-Keystone-Invocation-Id"] = str(self.identifier)
        response = await self.source.handle_async_request(request)
        self.observation.rate_limited = response.status_code == 429
        if self.observation.rate_limited:
            self.observation.retry_after_seconds = retry_after(response.headers.get("retry-after"))
        self.observation.streaming = "text/event-stream" in response.headers.get("content-type", "")
        self.observation.receipt_ids = {
            name: identifier
            for name in ("x-api-request-id", "x-oneapi-request-id", "x-request-id")
            if (identifier := request_id(response.headers.get(name)))
        }
        self.observation.on_update(self.observation)
        if self.observation.receipt_ids or self.observation.rate_limited:
            try:
                await self.on_receipt()
            except BaseException:
                await response.aclose()
                raise
        return ObservedResponse(response, request, self.observation)

    async def aclose(self):
        await self.source.aclose()

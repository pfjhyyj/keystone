"""Bounded protocol observation. Billing meaning is assigned by the caller."""

import json
from decimal import Decimal

import httpx2

from keystone.core.errors import ServiceError
from keystone.core.time import utcnow
from keystone.platform.model_access.adapters.inference.pricing import merge_usage, request_context
from keystone.platform.model_access.channels.shared.limits import retry_after
from keystone.platform.model_access.channels.shared.receipts import request_id


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
        # Relays name the same lifetime split differently; keep it as reported, never derived.
        "claude_cache_creation_5_m_tokens": "cache_write_5m_input_tokens",
        "claude_cache_creation_1_h_tokens": "cache_write_1h_input_tokens",
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
    # OpenAI-compatible explicit cache reports the write count under several names;
    # they describe the same tokens, so statistics keep their maximum and never sum them.
    details = data.get("prompt_tokens_details")
    details = details if isinstance(details, dict) else {}
    writes = [
        value
        for value in (
            details.get("cache_write_tokens"),
            details.get("cached_creation_tokens"),
            details.get("cache_creation_input_tokens"),
        )
        if type(value) is int and 0 <= value < 2**63
    ]
    if writes:
        result["cache_write_input_tokens"] = max(writes)
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
        self.phase = "preparing"
        self.request_bytes = None
        self.upstream_status = None
        self.pricing_profile = None
        self.pricing_context = {}
        self.pricing_usage = {}
        self.pricing_usage_final = False
        self.pricing_generated = None
        self.pricing_success = False
        self.pricing_observed_at = None

    def parse(self, raw):
        if raw == b"[DONE]":
            self.done = True
            self.pricing_observed_at = utcnow()
            return
        try:
            payload = json.loads(raw, parse_float=Decimal)
            if not isinstance(payload, dict):
                return
            identifier = request_id(payload.get("id"))
            if identifier:
                self.receipt_ids["body_id"] = identifier
            choices = payload.get("choices")
            terminal_reasons = {"stop", "length", "tool_calls", "function_call", "content_filter"}
            terminal_usage = choices == [] or (
                isinstance(choices, list)
                and bool(choices)
                and all(
                    isinstance(c, dict)
                    and isinstance(c.get("finish_reason"), str)
                    and c["finish_reason"] in terminal_reasons
                    for c in choices
                )
            )
            if (
                self.streaming
                and isinstance(choices, list)
                and any(isinstance(c, dict) and bool(c.get("delta")) for c in choices)
            ):
                self.pricing_usage_final = False
            data = payload.get("usage")
            if isinstance(data, dict):
                self.usage = usage(data)
                self.reported_cost = data.get("cost")
                incoming_usage = merge_usage({}, data)
                if incoming_usage:
                    # A final receipt cannot inherit stale subcounts from an earlier chunk.
                    self.pricing_usage = incoming_usage
                    self.pricing_usage_final = not self.streaming or terminal_usage
            self.on_update(self)
            # OpenRouter can report an upstream error inside an HTTP 200 SSE response.
            # Preserve receipts above, but never expose its raw error text or mark success.
            choices = payload.get("choices")
            if "error" in payload or (
                isinstance(choices, list)
                and any(isinstance(c, dict) and c.get("finish_reason") == "error" for c in choices)
            ):
                raise ServiceError(502, "model_upstream_error", "上游返回错误，结果与费用可能未知")
            if isinstance(choices, list) and any(
                isinstance(c, dict)
                and (
                    isinstance(c.get("message"), dict)
                    and c["message"].get("role") == "assistant"
                    and (
                        isinstance(c["message"].get("content"), str)
                        or bool(c["message"].get("tool_calls"))
                    )
                    or isinstance(c.get("finish_reason"), str)
                    and c["finish_reason"] in terminal_reasons
                )
                for c in choices
            ):
                self.pricing_success = True
            items = payload.get("data")
            if self.pricing_profile and isinstance(items, list) and items:
                if "usage.generated_images" in self.pricing_profile.quantities:
                    if len(items) <= 2 and all(
                        isinstance(item, dict)
                        and any(
                            isinstance(item.get(key), str) and item[key]
                            for key in ("url", "b64_json")
                        )
                        for item in items
                    ):
                        self.pricing_generated = len(items)
                        self.pricing_success = True
                elif "usage.output_total_tokens" in self.pricing_profile.structural_zero and all(
                    isinstance(item, dict)
                    and isinstance(item.get("embedding"), list)
                    and item["embedding"]
                    for item in items
                ):
                    self.pricing_success = True
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
        self.observation.phase = "reading_response"
        async for chunk in super().aiter_bytes(chunk_size):
            self.observation.feed(chunk)
            yield chunk
        if not self.observation.streaming and self.status_code < 400:
            self.observation.parse(bytes(self.observation.buffer))
        self.observation.buffer.clear()
        self.observation.phase = "response_received"
        if not self.observation.streaming or self.observation.done:
            self.observation.pricing_observed_at = utcnow()


class SendTransport(httpx2.AsyncBaseTransport):
    def __init__(
        self,
        source,
        before_send,
        observation,
        on_receipt,
        validate,
        identifier,
        *,
        authorize,
        max_request_bytes=12 * 1024 * 1024,
    ):
        self.source, self.before_send, self.observation = source, before_send, observation
        self.on_receipt, self.validate, self.identifier = on_receipt, validate, identifier
        self.authorize = authorize
        self.max_request_bytes = max_request_bytes

    async def handle_async_request(self, request):
        self.observation.phase = "validating_request"
        # SDK multipart requests stream by default. Bound the actual encoded body before
        # registration, then validate exactly the bytes that will reach the relay.
        try:
            content = request.content
        except httpx2.RequestNotRead:
            content = bytearray()
            async for chunk in request.stream:
                if len(content) + len(chunk) > self.max_request_bytes:
                    raise ServiceError(
                        413, "model_input_too_large", "模型请求超过本操作上限"
                    ) from None
                content.extend(chunk)
            request = httpx2.Request(
                request.method,
                request.url,
                headers=request.headers,
                content=bytes(content),
                extensions=request.extensions,
            )
        if len(content) > self.max_request_bytes:
            raise ServiceError(413, "model_input_too_large", "模型请求超过本操作上限")
        self.observation.request_bytes = len(content)
        self.validate(request)
        if self.observation.pricing_profile is not None:
            # Pricing evidence is best effort; freeze_pricing records None as pricing_invalid.
            try:
                self.observation.pricing_context = request_context(
                    request, self.observation.pricing_profile
                )
            except Exception:
                self.observation.pricing_context = None
        self.observation.phase = "admission"
        secret = await self.before_send()
        # Authorization and committed registration precede the only physical send.
        request.headers.pop("Authorization", None)
        self.authorize(request, secret)
        request.headers["X-Keystone-Invocation-Id"] = str(self.identifier)
        self.observation.phase = "sending"
        response = await self.source.handle_async_request(request)
        self.observation.phase = "response_headers"
        self.observation.upstream_status = response.status_code
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

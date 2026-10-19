"""Adapted from Ether's decoded-byte observation; authorization is at the actual send."""

import json
from decimal import Decimal

import httpx2

from keystone.core.errors import ServiceError
from keystone.platform.model_access import providers


class Observation:
    def __init__(self, provider):
        self.provider = providers.PROVIDERS[provider]
        self.streaming = self.done = False
        self.buffer = bytearray()
        self.total_bytes = 0
        self.upstream_request_id = None
        self.usage = None
        self.cost_amount = None

    def parse(self, raw):
        if raw == b"[DONE]":
            self.done = True
            return
        try:
            payload = json.loads(raw, parse_float=Decimal)
            if not isinstance(payload, dict):
                return
            self.upstream_request_id = (
                providers.request_id(payload.get("id")) or self.upstream_request_id
            )
            data = payload.get("usage")
            if isinstance(data, dict):
                self.usage = providers.usage(data)
                cost = self.provider.cost(data)
                if cost is not None:
                    self.cost_amount = cost
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
        if self.total_bytes > 4 * 1024 * 1024:
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
    def __init__(self, source, before_send, observation):
        self.source, self.before_send, self.observation = source, before_send, observation

    async def handle_async_request(self, request):
        await self.before_send(request)
        response = await self.source.handle_async_request(request)
        self.observation.streaming = "text/event-stream" in response.headers.get("content-type", "")
        self.observation.upstream_request_id = providers.request_id(
            response.headers.get("x-request-id")
        )
        return ObservedResponse(response, request, self.observation)

    async def aclose(self):
        await self.source.aclose()

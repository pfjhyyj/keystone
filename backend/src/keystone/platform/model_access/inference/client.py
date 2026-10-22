"""Controlled OpenAI-compatible protocol; authorization and persistence stay in Service."""

import asyncio
import json
from contextlib import asynccontextmanager

from openai import AsyncOpenAI, DefaultAsyncHttpxClient

from keystone.core.errors import ServiceError

from .transport import SendTransport

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
SUPPORTED_TARGETS = {
    "openrouter": (OPENROUTER_BASE, {"/chat/completions": "POST"}),
    "openlux": ("https://api.openlux.ai/v1", {"/chat/completions": "POST"}),
    "yunwu": ("https://yunwu.ai/v1", {"/chat/completions": "POST"}),
}


class InferenceClient:
    def __init__(self, http):
        self.http = http
        self.slots = asyncio.Semaphore(4)

    def check_provider(self, provider):
        if provider not in SUPPORTED_TARGETS:
            raise ServiceError(503, "model_provider_unavailable", "当前服务商尚未开放发送")

    def check_target(self, provider, base, path, *, method="POST"):
        self.check_provider(provider)
        allowed_base, paths = SUPPORTED_TARGETS[provider]
        if base != allowed_base or paths.get(path) != method:
            raise ServiceError(
                409, "model_target_not_allowed", "渠道须使用受支持的官方地址、路径和方法"
            )

    def transport(self, provider):
        self.check_provider(provider)
        return self.http.transport(provider)

    def validate_request(self, request, provider, selected):
        self.check_target(provider, selected.api_base, "/chat/completions")
        if len(request.content) > 262144:
            raise ServiceError(413, "model_input_too_large", "模型请求超过 256 KiB")
        if (
            request.method != "POST"
            or str(request.url) != selected.api_base + "/chat/completions"
            or json.loads(request.content).get("model") != selected.provider_model
        ):
            raise ServiceError(409, "model_target_changed", "模型请求目标发生变化")

    @asynccontextmanager
    async def connect(self, provider, selected, observation, before_send, on_receipt, identifier):
        self.check_target(provider, selected.api_base, "/chat/completions")
        transport = SendTransport(
            self.transport(provider),
            before_send,
            observation,
            on_receipt,
            lambda request: self.validate_request(request, provider, selected),
            identifier,
        )
        async with (
            self.slots,
            asyncio.timeout(60),
            DefaultAsyncHttpxClient(
                transport=transport, timeout=30, follow_redirects=False, trust_env=False
            ) as http,
            AsyncOpenAI(
                api_key="runtime-resolved",
                base_url=selected.api_base,
                http_client=http,
                max_retries=0,
                timeout=30,
            ) as sdk,
        ):
            yield sdk

    async def events(
        self,
        provider,
        selected,
        observation,
        before_send,
        on_receipt,
        identifier,
        *,
        data=None,
        sdk_request=None,
    ):
        async with self.connect(
            provider, selected, observation, before_send, on_receipt, identifier
        ) as sdk:
            args = (
                dict(
                    model=selected.provider_model,
                    messages=[m.model_dump() for m in data.messages],
                    max_tokens=data.max_tokens,
                )
                if data is not None
                else None
            )
            if sdk_request is not None:
                # Trusted Agent adapter: same one-send boundary, with tool/structured output.
                content = await sdk_request(sdk)
            elif data.stream:
                complete = False
                stream = await sdk.chat.completions.create(
                    **args, stream=True, stream_options={"include_usage": True}
                )
                yield {"type": "start", "invocation_id": str(identifier)}
                async with stream:
                    async for chunk in stream:
                        for choice in chunk.choices:
                            if choice.finish_reason is not None:
                                complete = True
                            if choice.delta.content:
                                yield {"type": "delta", "text": choice.delta.content}
                if not observation.done or not complete:
                    raise ServiceError(
                        502, "model_stream_incomplete", "流式响应未正常结束，结果可能未知"
                    )
                content = None
            else:
                response = await sdk.chat.completions.create(**args)
                if not response.choices or response.choices[0].finish_reason is None:
                    raise ServiceError(502, "model_response_unknown", "模型响应缺少完成结果")
                content = response.choices[0].message.content
                if not isinstance(content, str):
                    raise ServiceError(502, "model_response_unknown", "模型未返回文本结果")
            yield {"type": "result", "content": content}

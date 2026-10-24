"""Controlled OpenAI-compatible protocol; authorization and persistence stay in Service."""

import asyncio
import json
from contextlib import asynccontextmanager

from openai import AsyncOpenAI, DefaultAsyncHttpxClient

from keystone.core.errors import ServiceError
from keystone.platform.model_access.adapters.inference.kling import MODELS as KLING_MODELS
from keystone.platform.model_access.adapters.inference.transport import SendTransport
from keystone.platform.model_access.schemas.budgets import TEXT_OPERATIONS, ModelRequestBudget
from keystone.platform.model_access.schemas.structured import JsonOutput, StructuredRequest

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
SUPPORTED_TARGETS = {
    "openrouter": (OPENROUTER_BASE, {"/chat/completions": "POST", "/images": "POST"}),
    "openlux": (
        "https://api.openlux.ai/v1",
        {
            "/chat/completions": "POST",
            "/kling/v1/images/generations": "POST",
            "/kling/v1/images/generations/{id}": "GET",
        },
    ),
    "yunwu": ("https://yunwu.ai/v1", {"/chat/completions": "POST"}),
}

# Only implemented wire protocols are advertised. Entries cannot enable an adapter.
SUPPORTED_OPERATIONS = {provider: frozenset({"text.generate"}) for provider in SUPPORTED_TARGETS}
SUPPORTED_OPERATIONS["openrouter"] = frozenset(
    {"text.generate", "text.structured", "image.understand", "image.generate", "image.edit"}
)

SUPPORTED_OPERATIONS["openlux"] = frozenset({"text.generate", "image.generate"})
SUPPORTED_MODELS = {("openlux", "image.generate"): frozenset(KLING_MODELS)}


def operation_delivery(provider, operation):
    return "async" if provider == "openlux" and operation == "image.generate" else "sync"


def protocol_supports(provider, operation, delivery):
    if (
        operation not in SUPPORTED_OPERATIONS.get(provider, ())
        or operation_delivery(provider, operation) != delivery
    ):
        return False
    target = SUPPORTED_TARGETS.get(provider)
    if target is None:
        return False
    if delivery == "async":
        required = {
            "/kling/v1/images/generations": "POST",
            "/kling/v1/images/generations/{id}": "GET",
        }
    else:
        path = "/images" if operation in {"image.generate", "image.edit"} else "/chat/completions"
        required = {path: "POST"}
    return all(target[1].get(path) == method for path, method in required.items())


class InferenceClient:
    def __init__(self, http):
        self.http = http
        self.slots = asyncio.Semaphore(4)

    def check_provider(self, provider):
        if provider not in SUPPORTED_TARGETS:
            raise ServiceError(503, "model_provider_unavailable", "当前服务商尚未开放发送")

    def check_operation(self, provider, operation, delivery="sync", *, model=None):
        self.check_provider(provider)
        if (
            operation not in SUPPORTED_OPERATIONS.get(provider, ())
            or operation_delivery(provider, operation) != delivery
            or (provider, operation) in SUPPORTED_MODELS
            and model not in SUPPORTED_MODELS[provider, operation]
        ):
            raise ServiceError(409, "model_operation_unavailable", "协议不支持所需操作")

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

    def request_target(self, provider, selected):
        if selected.delivery == "async":
            from keystone.platform.model_access.adapters.inference.kling import BASE, ORIGIN, SUBMIT

            if (
                provider != "openlux"
                or selected.api_base != BASE
                or selected.operation != "image.generate"
            ):
                raise ServiceError(409, "model_operation_unavailable", "原生任务协议不受支持")
            self.check_target(provider, selected.api_base, SUBMIT)
            return ORIGIN, SUBMIT, "model_name"
        path = (
            "/images"
            if selected.operation in {"image.generate", "image.edit"}
            else "/chat/completions"
        )
        self.check_target(provider, selected.api_base, path)
        return selected.api_base, path, "model"

    def validate_request(self, request, provider, selected):
        base, path, model_field = self.request_target(provider, selected)
        limit = (
            12 * 1024 * 1024 if selected.operation in {"image.understand", "image.edit"} else 262144
        )
        if len(request.content) > limit:
            raise ServiceError(413, "model_input_too_large", "模型请求超过本操作上限")
        payload = json.loads(request.content)
        if (
            request.method != "POST"
            or str(request.url) != base + path
            or payload.get(model_field) != selected.provider_model
        ):
            raise ServiceError(409, "model_target_changed", "模型请求目标发生变化")

        if selected.operation in {"text.generate", "text.structured", "image.understand"}:
            if payload.get("modalities", ["text"]) != ["text"] or "audio" in payload:
                raise ServiceError(422, "model_operation_unavailable", "该角色不允许媒体输出")
            for message in payload.get("messages", ()):
                content = message.get("content")
                if isinstance(content, list) and any(
                    part.get("type")
                    not in (
                        {"text", "image_url"}
                        if selected.operation == "image.understand"
                        else {"text"}
                    )
                    for part in content
                ):
                    raise ServiceError(
                        422, "model_operation_unavailable", "该角色不允许此类媒体输入"
                    )
            if any(tool.get("type") != "function" for tool in payload.get("tools", ())):
                raise ServiceError(422, "model_operation_unavailable", "该角色不允许原生媒体工具")
            output = payload.get("response_format", {})
            if output.get("type") == "json_schema":
                if selected.operation != "text.structured":
                    raise ServiceError(
                        422, "model_operation_unavailable", "结构化输出需要明确角色能力"
                    )
                try:
                    spec = output["json_schema"]
                    JsonOutput(name=spec["name"], json_schema=spec["schema"])
                    if (
                        spec.get("strict") is not True
                        or provider == "openrouter"
                        and payload.get("provider", {}).get("require_parameters") is not True
                    ):
                        raise ValueError()
                except (KeyError, TypeError, ValueError):
                    raise ServiceError(
                        422, "model_invalid_request", "结构化输出须使用受控严格 Schema"
                    ) from None
        maximum = 0
        if selected.operation in TEXT_OPERATIONS:
            if (
                type(payload.get("n", 1)) is not int
                or payload.get("n", 1) != 1
                or "best_of" in payload
            ):
                raise ServiceError(422, "model_invalid_request", "文本操作只允许单份输出")
            maximum = payload.get("max_completion_tokens", payload.get("max_tokens"))
            if (
                type(maximum) is not int
                or not 1 <= maximum <= 4096
                or "max_completion_tokens" in payload
                and "max_tokens" in payload
            ):
                raise ServiceError(
                    422, "model_invalid_request", "文本请求须明确受控输出 token 上限"
                )
        return ModelRequestBudget(operation=selected.operation, max_output_tokens=maximum)

    @asynccontextmanager
    async def connect(self, provider, selected, observation, before_send, on_receipt, identifier):
        base, _, _ = self.request_target(provider, selected)
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
                base_url=base,
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
            if isinstance(data, StructuredRequest):
                args["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": data.output.name,
                        "strict": True,
                        "schema": data.output.json_schema,
                    },
                }
                args["extra_body"] = {"provider": {"require_parameters": True}}
            if sdk_request is not None:
                # Trusted Agent adapter: same one-send boundary, with tool/structured output.
                content = await sdk_request(sdk)
            elif getattr(data, "stream", False):
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

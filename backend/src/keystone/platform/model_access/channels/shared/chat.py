"""Explicit wire implementations; no authorization, persistence or scheduler."""

import json
from dataclasses import dataclass

from keystone.platform.model_access.channels.shared import images
from keystone.platform.model_access.schemas.errors import AdapterError
from keystone.platform.model_access.schemas.invocations import MAX_OUTPUT_TOKENS
from keystone.platform.model_access.schemas.structured import JsonOutput, StructuredRequest


def validate_request(request, selected, target, *, require_parameters=False, image_count=0):
    """image_count is the host-approved number of inline images for structured output."""
    base, path, model_field = target
    if image_count and selected.operation != "text.structured":
        raise AdapterError(422, "model_operation_unavailable", "该角色不允许附加图片")
    media = selected.operation in {"image.understand", "image.edit"} or image_count
    limit = 12 * 1024 * 1024 if media else 262144
    if len(request.content) > limit:
        raise AdapterError(413, "model_input_too_large", "模型请求超过本操作上限")
    payload = json.loads(request.content)
    if (
        request.method != "POST"
        or str(request.url) != base + path
        or payload.get(model_field) != selected.provider_model
    ):
        raise AdapterError(409, "model_target_changed", "模型请求目标发生变化")

    if selected.operation in {"text.generate", "text.structured", "image.understand"}:
        if payload.get("modalities", ["text"]) != ["text"] or "audio" in payload:
            raise AdapterError(422, "model_operation_unavailable", "该角色不允许媒体输出")
        allowed = {"text", "image_url"} if media else {"text"}
        inline = 0
        for message in payload.get("messages", ()):
            content = message.get("content")
            if isinstance(content, list) and any(
                part.get("type") not in allowed for part in content
            ):
                raise AdapterError(422, "model_operation_unavailable", "该角色不允许此类媒体输入")
            for part in content if isinstance(content, list) else ():
                if part.get("type") == "image_url":
                    url = (part.get("image_url") or {}).get("url")
                    if image_count and not (isinstance(url, str) and url.startswith("data:image/")):
                        raise AdapterError(
                            422, "model_invalid_request", "结构化图片须为本次已校验的内联图片"
                        )
                    inline += 1
        if image_count and inline != image_count:
            raise AdapterError(422, "model_invalid_request", "结构化请求的图片数量与受理请求不一致")
        if any(tool.get("type") != "function" for tool in payload.get("tools", ())):
            raise AdapterError(422, "model_operation_unavailable", "该角色不允许原生媒体工具")
        output = payload.get("response_format", {})
        if output.get("type") == "json_schema":
            if selected.operation != "text.structured":
                raise AdapterError(422, "model_operation_unavailable", "结构化输出需要明确角色能力")
            try:
                spec = output["json_schema"]
                JsonOutput(name=spec["name"], json_schema=spec["schema"])
                if (
                    spec.get("strict") is not True
                    or require_parameters
                    and payload.get("provider", {}).get("require_parameters") is not True
                ):
                    raise ValueError()
            except (KeyError, TypeError, ValueError):
                raise AdapterError(
                    422, "model_invalid_request", "结构化输出须使用受控严格 Schema"
                ) from None
    if selected.operation in {"text.generate", "text.structured", "image.understand"}:
        if type(payload.get("n", 1)) is not int or payload.get("n", 1) != 1 or "best_of" in payload:
            raise AdapterError(422, "model_invalid_request", "文本操作只允许单份输出")
        maximum = payload.get("max_completion_tokens", payload.get("max_tokens"))
        if (
            type(maximum) is not int
            or not 1 <= maximum <= MAX_OUTPUT_TOKENS
            or "max_completion_tokens" in payload
            and "max_tokens" in payload
        ):
            raise AdapterError(422, "model_invalid_request", "文本请求须明确受控输出 token 上限")


def structured_format(data):
    return {
        "type": "json_schema",
        "json_schema": {
            "name": data.output.name,
            "strict": True,
            "schema": data.output.json_schema,
        },
    }


@dataclass(frozen=True)
class ChatCompletions:
    operations: frozenset = frozenset({"text.generate"})
    require_parameters: bool = False
    pricing_meter: str = "chat.v3"
    # Official OpenAI reasoning models reject the legacy max_tokens name.
    token_limit: str = "max_tokens"
    delivery = "sync"
    models = None
    paths = {"/chat/completions": "POST"}

    def request_target(self, selected):
        return selected.api_base, "/chat/completions", "model"

    def reasoning_settings(self, operation, model, reasoning):
        if reasoning is not None:
            raise AdapterError(422, "model_invalid_request", "该模型协议不支持显式思考设置")
        return {}

    def sdk_settings(self, operation, settings, *, model=None, reasoning=None):
        controls = self.reasoning_settings(operation, model, reasoning)
        if controls:
            settings = {
                **settings,
                "extra_body": {**(settings.get("extra_body") or {}), **controls},
            }
        if operation != "text.structured" or not self.require_parameters:
            return settings
        extra = settings.get("extra_body") or {}
        preferences = extra.get("provider") or {}
        return {
            **settings,
            "extra_body": {**extra, "provider": {**preferences, "require_parameters": True}},
        }

    async def events(self, sdk, selected, observation, identifier, *, data=None):
        args = (
            dict(
                model=selected.provider_model,
                messages=[m.model_dump() for m in data.messages],
                **{self.token_limit: data.max_tokens},
            )
            if data is not None
            else None
        )
        if isinstance(data, StructuredRequest):
            args["response_format"] = structured_format(data)
        if args is not None:
            args = self.sdk_settings(
                selected.operation, args, model=selected.provider_model, reasoning=data.reasoning
            )
        if getattr(data, "stream", False):
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
                raise AdapterError(
                    502, "model_stream_incomplete", "流式响应未正常结束，结果可能未知"
                )
            content = None
        else:
            response = await sdk.chat.completions.create(**args)
            if not response.choices or response.choices[0].finish_reason is None:
                raise AdapterError(502, "model_response_unknown", "模型响应缺少完成结果")
            content = response.choices[0].message.content
            if not isinstance(content, str):
                raise AdapterError(502, "model_response_unknown", "模型未返回文本结果")
        yield {"type": "result", "content": content}

    async def vision(self, sdk, selected, data, inputs, run_sync):
        parts = []
        for (content_type, content, _), image in zip(inputs, data.images, strict=True):
            encoded = await run_sync(images.image_data_uri, content, content_type)
            parts.append(
                {"type": "image_url", "image_url": {"url": encoded, "detail": image.detail}}
            )
        messages = [message.model_dump(mode="json") for message in data.messages]
        messages.append({"role": "user", "content": parts})
        args = {
            "model": selected.provider_model,
            "messages": messages,
            self.token_limit: data.max_tokens,
        }
        if isinstance(data, StructuredRequest):
            # Same strict schema as text-only structured output; parsing stays with the caller.
            args["response_format"] = structured_format(data)
        response = await sdk.chat.completions.create(
            **self.sdk_settings(
                selected.operation, args, model=selected.provider_model, reasoning=data.reasoning
            )
        )
        if not response.choices or response.choices[0].finish_reason is None:
            raise AdapterError(502, "model_response_unknown", "模型响应缺少完成结果")
        content = response.choices[0].message.content
        if not isinstance(content, str):
            raise AdapterError(502, "model_response_unknown", "模型未返回文本结果")
        return content

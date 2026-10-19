"""One SDK send per registered attempt. No message/output persistence or automatic retries.

Adapts Ether's managed SDK/transport boundary; Keystone registers before dispatch and
uses the invocation PK to refuse a repeated durable request even after a successful call.
"""

import asyncio
import json
from contextlib import aclosing
from uuid import uuid4

import httpx2
from openai import APIStatusError, AsyncOpenAI, DefaultAsyncHttpxClient
from sqlalchemy.dialects.postgresql import insert

from keystone.core.errors import ServiceError
from keystone.core.time import utcnow
from keystone.platform.model_access.models import Channel, ModelEntry, ModelInvocation
from keystone.platform.model_access.services.queries import locked
from keystone.platform.model_access.transport import Observation, SendTransport


def classify_error(error):
    """One sanitized classification for public errors and persisted attempt status."""
    from pydantic_ai.exceptions import ModelHTTPError

    causes, seen = [], set()
    cause = error
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        causes.append(cause)
        cause = cause.__cause__
    # Preserve platform errors even when an SDK wraps them; no raw upstream message escapes.
    for cause in causes:
        if isinstance(cause, ServiceError):
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


def safe_error(error):
    return classify_error(error)[:3]


async def completed(events):
    async with aclosing(events):
        async for event in events:
            if event["type"] == "error":
                raise ServiceError(event["http_status"], event["code"], event["detail"])
            if event["type"] == "done":
                return event


async def settle(coroutine):
    """Finish only this known attempt after cancellation; bounded and never detached."""

    async def bounded():
        async with asyncio.timeout(5):
            return await coroutine

    task = asyncio.create_task(bounded())
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


class ModelRuntime:
    def __init__(self, service):
        self.service = service
        self.identity, self.gateway = service.identity, service.gateway

    async def invoke(self, data, *, invocation_id=None, selection=None):
        return await completed(self.events(data, invocation_id=invocation_id, selection=selection))

    async def invoke_sdk(self, *, selection, invocation_id, sdk_request, flow_context=None):
        """Trusted internal request, with a fixed target and stable ID; no public Invoke payload."""
        if invocation_id is None or not callable(sdk_request):
            raise ValueError("A trusted SDK call requires a stable ID and request callback")
        result = await completed(
            self._events(
                selection,
                invocation_id=invocation_id,
                flow_context=flow_context,
                sdk_request=sdk_request,
            )
        )
        return result["content"]

    async def events(self, data, *, invocation_id=None, selection=None):
        selected = selection or await self.service.selection(data.model_key)
        async with aclosing(
            self._events(selected, data=data, invocation_id=invocation_id)
        ) as events:
            async for event in events:
                yield event

    async def _events(
        self, selected, *, data=None, invocation_id=None, flow_context=None, sdk_request=None
    ):
        identifier = invocation_id or uuid4()
        async with self.identity.database.transaction() as session:
            channel = await session.get(Channel, selected.channel_id)
            if channel is None:
                raise ServiceError(404, "model_not_found", "渠道配置不存在")
            provider = channel.provider
        observation = Observation(provider)
        registered = finished = False
        self.gateway.check_target(provider, selected.api_base, "/chat/completions")
        source = self.gateway.transport(provider)

        async def before_send(request):
            nonlocal registered
            self.gateway.check_target(provider, selected.api_base, "/chat/completions")
            if len(request.content) > 262144:
                raise ServiceError(413, "model_input_too_large", "模型请求超过 256 KiB")
            if registered:
                raise ServiceError(409, "model_result_unavailable", "同一次调用不能再次发送")
            if (
                request.method != "POST"
                or str(request.url) != selected.api_base + "/chat/completions"
                or json.loads(request.content).get("model") != selected.provider_model
            ):
                raise ServiceError(409, "model_target_changed", "模型请求目标发生变化")
            async with self.identity.access("model:invoke") as (session, auth):
                entry = await locked(session, ModelEntry, selected.model_entry_id)
                channel = await locked(session, Channel, selected.channel_id)
                if not entry.is_active or not channel.is_active:
                    raise ServiceError(409, "model_disabled", "模型或渠道已停用")
                if channel.api_base != selected.api_base:
                    raise ServiceError(
                        409, "model_configuration_changed", "渠道地址已改变，请重新选择模型"
                    )
                secret = self.gateway.secret(channel.credential_ref)
                created = await session.scalar(
                    insert(ModelInvocation)
                    .values(
                        id=identifier,
                        user_id=auth.user.id,
                        flow_context=flow_context.model_dump(mode="json", exclude_none=True)
                        if flow_context
                        else None,
                        model_entry_id=entry.id,
                        channel_id=channel.id,
                        provider_model=selected.provider_model,
                        status="running",
                        cost_source="unknown",
                        cost_note="awaiting_response",
                    )
                    .on_conflict_do_nothing(index_elements=[ModelInvocation.id])
                    .returning(ModelInvocation.id)
                )
                if created is None:
                    raise ServiceError(
                        409,
                        "model_result_unavailable",
                        "已有调用记录但没有可恢复结果，禁止重复发送",
                    )
            # Commit completed: only now does this executor own the right to send.
            registered = True
            request.headers["Authorization"] = "Bearer " + secret
            request.headers["X-Keystone-Invocation-Id"] = str(identifier)

        async def finish(status, code=None):
            nonlocal finished
            if not registered or finished:
                return
            try:
                async with self.identity.database.transaction() as session:
                    row = await locked(session, ModelInvocation, identifier)
                    if row.status != "running":
                        raise ServiceError(
                            409, "model_result_unavailable", "调用记录已完成，不能覆盖"
                        )
                    row.status, row.error_code = status, code
                    row.finished_at = row.updated_at = utcnow()
                    row.upstream_request_id, row.usage = (
                        observation.upstream_request_id,
                        observation.usage,
                    )
                    if observation.cost_amount is not None:
                        row.cost_amount = observation.cost_amount
                        row.cost_unit, row.cost_source = observation.provider.cost_unit, "response"
                        row.cost_note = None
                    else:
                        row.cost_note = "upstream_not_reported"
                    await session.flush()
                finished = True
            except Exception:
                raise ServiceError(
                    503, "model_result_unavailable", "调用记录补全失败，禁止自动重发"
                ) from None

        transport = SendTransport(source, before_send, observation)
        try:
            async with (
                self.gateway.slots,
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
                await finish("succeeded")
                yield {
                    "type": "done",
                    "invocation_id": str(identifier),
                    "content": content,
                }
        except BaseException as error:
            http_status, code, detail, status = classify_error(error)
            if not finished:
                try:
                    await settle(finish(status, code))
                except Exception:
                    http_status, code, detail = (
                        503,
                        "model_result_unavailable",
                        "调用记录补全失败，禁止自动重发",
                    )
            if isinstance(error, (asyncio.CancelledError, GeneratorExit)):
                raise
            yield {
                "type": "error",
                "http_status": http_status,
                "code": code,
                "detail": detail,
                "invocation_id": str(identifier) if registered else None,
            }
        finally:
            observation.buffer.clear()

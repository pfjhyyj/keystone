"""One SDK send per registered attempt. No message/output persistence or automatic retries.

Adapts Ether's managed SDK/transport boundary; Keystone registers before dispatch and
uses the invocation PK to refuse a repeated durable request even after a successful call.
"""

import asyncio
from contextlib import AsyncExitStack, aclosing
from uuid import uuid4

from sqlalchemy.dialects.postgresql import insert

from keystone.core.errors import ServiceError
from keystone.core.time import utcnow
from keystone.platform.model_access.adapters.billing.contracts import Receipt
from keystone.platform.model_access.adapters.inference.errors import classify_error
from keystone.platform.model_access.adapters.inference.transport import Observation
from keystone.platform.model_access.models import Channel, ModelEntry, ModelInvocation
from keystone.platform.model_access.services.cost_context import bind_channel
from keystone.platform.model_access.services.cost_policy import initialize_check
from keystone.platform.model_access.services.queries import locked


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
        self.identity, self.clients = service.identity, service.clients

    async def invoke(self, data, *, invocation_id=None, selection=None):
        return await completed(self.events(data, invocation_id=invocation_id, selection=selection))

    async def invoke_sdk(
        self, *, selection, invocation_id, sdk_request, flow_context=None, worker_identity=None
    ):
        """Trusted internal request, with a fixed target and stable ID; no public Invoke payload."""
        if invocation_id is None or not callable(sdk_request):
            raise ValueError("A trusted SDK call requires a stable ID and request callback")
        result = await completed(
            self._events(
                selection,
                invocation_id=invocation_id,
                flow_context=flow_context,
                sdk_request=sdk_request,
                worker_identity=worker_identity,
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
        self,
        selected,
        *,
        data=None,
        invocation_id=None,
        flow_context=None,
        sdk_request=None,
        worker_identity=None,
    ):
        identifier = invocation_id or uuid4()
        async with self.identity.database.transaction() as session:
            channel = await session.get(Channel, selected.channel_id)
            if channel is None:
                raise ServiceError(404, "model_not_found", "渠道配置不存在")
            provider = channel.provider
        receipt = Receipt()
        adapter = self.clients.billing.adapter(provider)

        def observed(observation):
            incoming = adapter.receipt(observation.receipt_ids, observation.reported_cost)
            if incoming.request_id:
                receipt.request_id = incoming.request_id
            if incoming.fee is not None:
                receipt.fee = incoming.fee

        observation = Observation(observed)
        registered = finished = False
        budget = AsyncExitStack()

        async def before_send():
            nonlocal registered
            if registered:
                raise ServiceError(409, "model_result_unavailable", "同一次调用不能再次发送")
            async with self.identity.access("model:invoke") as (session, _):
                original = await locked(session, Channel, selected.channel_id)
                budget_secret = self.clients.credentials.decrypt(
                    original.api_key_encrypted, original.id, "inference"
                )
                budget_scope = self.clients.credentials.scope(original.api_base, budget_secret)
            permit = await budget.enter_async_context(
                self.clients.coordination.inference(budget_scope)
            )
            async with self.identity.access("model:invoke") as (session, auth):
                entry = await locked(session, ModelEntry, selected.model_entry_id)
                channel = await locked(session, Channel, selected.channel_id)
                if not entry.is_active or not channel.is_active:
                    raise ServiceError(409, "model_disabled", "模型或渠道已停用")
                if channel.api_base != selected.api_base:
                    raise ServiceError(
                        409, "model_configuration_changed", "渠道地址已改变，请重新选择模型"
                    )
                secret = self.clients.credentials.decrypt(
                    channel.api_key_encrypted, channel.id, "inference"
                )
                if secret != budget_secret:
                    raise ServiceError(
                        409, "model_configuration_changed", "渠道密钥已改变，请重新选择模型"
                    )
                if flow_context is not None and worker_identity is None:
                    raise ServiceError(
                        503, "model_worker_unavailable", "Flow 模型发送缺少可信执行归属"
                    )
                created = await session.scalar(
                    insert(ModelInvocation)
                    .values(
                        id=identifier,
                        worker_id=worker_identity.worker_id if worker_identity else None,
                        worker_instance_id=worker_identity.instance_id if worker_identity else None,
                        execution_version=worker_identity.execution_version
                        if worker_identity
                        else None,
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
                        cost_context=bind_channel(channel, self.clients),
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
            await permit()
            return secret

        async def save_receipt():
            if not receipt.request_id:
                return
            try:
                async with self.identity.database.transaction() as session:
                    row = await locked(session, ModelInvocation, identifier)
                    if row.status == "running":
                        row.upstream_request_id = receipt.request_id
                        row.receipt_ids = dict(observation.receipt_ids)
            except Exception:
                raise ServiceError(
                    503, "model_result_unavailable", "调用回执保存失败，禁止重新发送"
                ) from None

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
                        receipt.request_id,
                        observation.usage,
                    )
                    row.receipt_ids = dict(observation.receipt_ids) or None
                    if receipt.fee is not None:
                        row.cost_amount = receipt.fee.amount
                        row.cost_unit, row.cost_source = receipt.fee.unit, "response"
                        row.cost_note = None
                    else:
                        row.cost_note = "upstream_not_reported"
                    initialize_check(row, row.finished_at)
                    await session.flush()
                finished = True
            except Exception:
                raise ServiceError(
                    503, "model_result_unavailable", "调用记录补全失败，禁止自动重发"
                ) from None

        try:
            async with aclosing(
                self.clients.inference.events(
                    provider,
                    selected,
                    observation,
                    before_send,
                    save_receipt,
                    identifier,
                    data=data,
                    sdk_request=sdk_request,
                )
            ) as events:
                async for event in events:
                    if event["type"] != "result":
                        yield event
                        continue
                    await finish("succeeded")
                    yield {
                        "type": "done",
                        "invocation_id": str(identifier),
                        "content": event["content"],
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
            await budget.aclose()
            observation.buffer.clear()

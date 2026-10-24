"""One SDK send per registered attempt. No message/output persistence or automatic retries.

Registers before dispatch and uses the invocation PK to refuse a repeated durable request
even after a successful call.
"""

import asyncio
import hashlib
import json
from contextlib import AsyncExitStack, aclosing, nullcontext
from time import monotonic
from uuid import uuid4

from sqlalchemy.dialects.postgresql import insert

from keystone.core.errors import ServiceError
from keystone.core.time import utcnow
from keystone.platform.model_access.adapters.billing.contracts import Receipt
from keystone.platform.model_access.adapters.inference.errors import classify_error
from keystone.platform.model_access.adapters.inference.transport import Observation
from keystone.platform.model_access.models import Channel, ModelEntry, ModelInvocation
from keystone.platform.model_access.schemas.images import (
    ImageBatch,
    ImageDownloadReceipt,
    ImageManifest,
    ImageSources,
    ImageTaskReceipt,
)
from keystone.platform.model_access.services.cost_context import bind_channel
from keystone.platform.model_access.services.cost_policy import initialize_check
from keystone.platform.model_access.services.diagnostics import log_failure
from keystone.platform.model_access.services.dispatch import ModelDispatchService
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
    def __init__(self, service, *, admission=None, settlement=None, operation_scope=None):
        self.service = service
        self.identity, self.clients = service.identity, service.clients
        self.dispatch = ModelDispatchService(self.identity.database, self.identity.settings)
        self.admission, self.settlement = admission, settlement
        self.operation_scope = operation_scope

    async def invoke(self, data, *, invocation_id=None, selection=None):
        return await completed(self.events(data, invocation_id=invocation_id, selection=selection))

    def sdk_settings(self, selection, settings):
        return self.clients.inference.sdk_settings(selection, settings)

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

    @staticmethod
    def image_digest(data, operation):
        values = data.model_dump(mode="json")
        # Preserve old digests: newly added optional controls were absent from old DTOs.
        for key in ("aspect_ratio", "quality"):
            if values.get(key) is None:
                values.pop(key, None)
        raw = json.dumps(
            [operation, values],
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        return hashlib.sha256(raw).hexdigest()

    async def image_manifest(self, identifier, *, data, selection, context):
        async with self.identity.access("model:invoke") as (session, _):
            row = await session.get(ModelInvocation, identifier)
            if row is None:
                return None
            expected = context.model_dump(mode="json", exclude_none=True)
            if (
                row.flow_context != expected
                or row.operation != selection.operation
                or row.model_entry_id != selection.model_entry_id
                or row.channel_id != selection.channel_id
                or row.provider_model != selection.provider_model
                or row.status != "succeeded"
            ):
                raise ServiceError(
                    409, "model_result_unavailable", "原图片调用无法恢复，禁止重新发送"
                )
            try:
                manifest = (
                    ImageDownloadReceipt.model_validate(row.result_context)
                    if row.result_context.get("kind") == "download"
                    else ImageManifest.model_validate(row.result_context)
                )
                if (
                    manifest.operation != selection.operation
                    or manifest.request_digest != self.image_digest(data, selection.operation)
                ):
                    raise ValueError()
                for index, output in enumerate(getattr(manifest, "outputs", ())):
                    if output.artifact_key != f"model.{identifier}.{index}":
                        raise ValueError()
                return manifest
            except (ValueError, TypeError, AttributeError):
                raise ServiceError(409, "model_result_unavailable", "原图片结果证据无效") from None

    def image_limits(self, selected):
        return self.clients.inference.image_limits(selected)

    async def recover_image(self, identifier, *, selection, context, evidence):
        from keystone.platform.model_access.services.image_results import ImageResultService

        return await ImageResultService(self.identity, self.clients).fetch(
            identifier, selected=selection, context=context, evidence=evidence
        )

    async def invoke_image(
        self, data, *, images, selection, invocation_id, context, worker_identity
    ):
        from keystone.platform.model_access.schemas.images import (
            ImageEditRequest,
            ImageGenerateRequest,
        )

        edit = isinstance(data, ImageEditRequest)
        operation = "image.edit" if edit else "image.generate"
        contract = ImageEditRequest if edit else ImageGenerateRequest
        data = contract.model_validate(data.model_dump(mode="json"))
        if selection.operation != operation or len(images) != (len(data.images) if edit else 0):
            raise ServiceError(422, "model_invalid_request", "图片操作与请求类型不匹配")
        max_images, max_bytes = self.image_limits(selection)
        if len(images) > max_images or sum(len(image[1]) for image in images) > max_bytes:
            raise ServiceError(413, "model_input_too_large", "图片累计大小超过上限")
        digest = self.image_digest(data, operation)

        async def send(sdk):
            return await self.clients.inference.image(
                sdk, selection, data, images, invocation_id, digest
            )

        result = await self.invoke_sdk(
            selection=selection,
            invocation_id=invocation_id,
            sdk_request=send,
            flow_context=context,
            worker_identity=worker_identity,
        )
        if isinstance(result, ImageSources):
            evidence = await self.image_manifest(
                invocation_id, data=data, selection=selection, context=context
            )
            return await self.recover_image(
                invocation_id, selection=selection, context=context, evidence=evidence
            )
        return result

    async def invoke_vision(
        self, data, *, images, selection, invocation_id, context, worker_identity
    ):
        from keystone.platform.model_access.schemas.images import (
            MAX_INPUT_IMAGE_BYTES,
            VisionRequest,
        )

        data = VisionRequest.model_validate(data.model_dump(mode="json"))
        if selection.operation != "image.understand" or len(images) != len(data.images):
            raise ServiceError(422, "model_invalid_request", "图片操作或输入不匹配")
        if sum(len(image[1]) for image in images) > MAX_INPUT_IMAGE_BYTES:
            raise ServiceError(413, "model_input_too_large", "图片累计大小超过上限")

        async def send(sdk):
            return await self.clients.inference.vision(sdk, selection, data, images)

        return await self.invoke_sdk(
            selection=selection,
            invocation_id=invocation_id,
            sdk_request=send,
            flow_context=context,
            worker_identity=worker_identity,
        )

    async def invoke_text(self, data, *, selection, invocation_id, context, worker_identity):
        from keystone.platform.model_access.schemas.invocations import TextRequest
        from keystone.platform.model_access.schemas.structured import StructuredRequest

        expected = "text.structured" if isinstance(data, StructuredRequest) else "text.generate"
        if selection.operation != expected:
            raise ServiceError(422, "model_invalid_request", "文本操作与请求类型不匹配")
        contract = StructuredRequest if expected == "text.structured" else TextRequest
        data = contract.model_validate(data.model_dump(mode="json"))
        return await completed(
            self._events(
                selection,
                data=data,
                invocation_id=invocation_id,
                flow_context=context,
                worker_identity=worker_identity,
            )
        )

    async def events(self, data, *, invocation_id=None, selection=None):
        selected = selection or await self.service.selection(data.model_key)
        async with aclosing(
            self._events(selected, data=data, invocation_id=invocation_id)
        ) as events:
            async for event in events:
                yield event

    async def _events(self, *args, **kwargs):
        async with self.operation_scope() if self.operation_scope is not None else nullcontext():
            async with aclosing(self._unscoped_events(*args, **kwargs)) as events:
                async for event in events:
                    yield event

    async def _unscoped_events(
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
        started = monotonic()
        registered = finished = False
        provider, observation = selected.provider, None
        receipt = Receipt()

        def failed(error, stage):
            log_failure(
                error,
                identifier=identifier,
                selected=selected,
                provider=provider,
                correlation_id=self.identity.correlation_id,
                started=started,
                stage=stage,
                registered=registered,
                finished=finished,
                observation=observation,
                upstream_request_id=receipt.request_id,
                flow_context=flow_context,
                worker_identity=worker_identity,
            )

        try:
            async with self.identity.database.transaction() as session:
                channel = await session.get(Channel, selected.channel_id)
                if channel is None:
                    raise ServiceError(404, "model_not_found", "渠道配置不存在")
                provider = channel.provider
                self.clients.inference.check_operation(
                    provider, selected.operation, selected.delivery, model=selected.provider_model
                )
            adapter = self.clients.billing.adapter(provider)
        except BaseException as error:
            failed(error, "preflight")
            raise

        def observed(observation):
            incoming = adapter.receipt(observation.receipt_ids, observation.reported_cost)
            if incoming.request_id:
                receipt.request_id = incoming.request_id
            if incoming.fee is not None:
                receipt.fee = incoming.fee

        observation = Observation(
            observed,
            max_bytes=24 * 1024 * 1024
            if selected.operation in {"image.generate", "image.edit"}
            else 4 * 1024 * 1024,
        )
        budget = AsyncExitStack()
        dispatch_scope = None

        async def before_send():
            nonlocal registered, dispatch_scope
            send_deadline = asyncio.get_running_loop().time() + 55
            if registered:
                raise ServiceError(409, "model_result_unavailable", "同一次调用不能再次发送")
            async with self.identity.access("model:invoke") as (session, _):
                original = await locked(session, Channel, selected.channel_id)
                budget_secret = self.clients.credentials.decrypt(
                    original.api_key_encrypted, original.id, "inference"
                )
                budget_scope = self.clients.credentials.scope(original.api_base, budget_secret)
                dispatch_scope = budget_scope
            permit = await budget.enter_async_context(
                self.clients.coordination.inference(budget_scope)
            )
            while True:
                async with self.identity.access("model:invoke") as (session, auth):
                    entry = await locked(session, ModelEntry, selected.model_entry_id)
                    channel = await locked(session, Channel, selected.channel_id)
                    if selected.operation not in entry.operations:
                        raise ServiceError(
                            409, "model_configuration_changed", "原模型操作能力已移除"
                        )
                    if not entry.is_active or not channel.is_active:
                        raise ServiceError(409, "model_disabled", "模型或渠道已停用")
                    if selected.provider is not None and (
                        entry.channel_id != selected.channel_id
                        or entry.provider_model != selected.provider_model
                        or channel.provider != selected.provider
                    ):
                        raise ServiceError(
                            409, "model_configuration_changed", "原模型或渠道配置已改变"
                        )
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
                    if await session.get(ModelInvocation, identifier) is not None:
                        raise ServiceError(
                            409, "model_result_unavailable", "已有调用记录，禁止再次发送"
                        )
                    delay = await self.dispatch.claim_in_transaction(session, budget_scope)
                    if delay == 0:
                        if self.admission is not None:
                            await self.admission(session, identifier, observation.request_budget)
                        created = await session.scalar(
                            insert(ModelInvocation)
                            .values(
                                id=identifier,
                                worker_id=worker_identity.worker_id if worker_identity else None,
                                worker_instance_id=worker_identity.instance_id
                                if worker_identity
                                else None,
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
                                operation=selected.operation,
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
                if delay == 0:
                    break
                remaining = send_deadline - asyncio.get_running_loop().time()
                if delay >= remaining:
                    raise ServiceError(429, "model_dispatch_delayed", "模型共享冷却尚未到期")
                await asyncio.sleep(min(delay, 1))
                await permit()
            # Commit completed: only now does this executor own the right to send.
            registered = True
            await permit()
            return secret

        async def save_receipt():
            if observation.rate_limited:
                await self.dispatch.cooldown(dispatch_scope, observation.retry_after_seconds)
            if not receipt.request_id:
                return
            try:
                async with self.identity.database.transaction() as session:
                    row = await locked(session, ModelInvocation, identifier)
                    if row.status == "running":
                        row.upstream_request_id = receipt.request_id
                        row.receipt_ids = dict(observation.receipt_ids)
            except Exception as error:
                failed(error, "persist_receipt")
                raise ServiceError(
                    503, "model_result_unavailable", "调用回执保存失败，禁止重新发送"
                ) from None

        async def finish(status, code=None, *, result_context=None):
            nonlocal finished
            if not registered or finished:
                return
            try:
                async with self.identity.database.transaction() as session:
                    if self.settlement is not None:
                        await self.settlement(session, identifier, observation.usage)
                    row = await locked(session, ModelInvocation, identifier)
                    if row.status != "running":
                        raise ServiceError(
                            409, "model_result_unavailable", "调用记录已完成，不能覆盖"
                        )
                    row.status, row.error_code = status, code
                    row.result_context = result_context
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
            except Exception as error:
                failed(error, "persist_result")
                raise ServiceError(
                    503, "model_result_unavailable", "调用记录补全失败，禁止自动重发"
                ) from None

        async def accepted_task(task):
            nonlocal finished
            async with self.identity.database.transaction() as session:
                row = await locked(session, ModelInvocation, identifier)
                if row.status != "running" or row.result_context is not None:
                    raise ServiceError(409, "model_result_unavailable", "原任务回执不能覆盖")
                row.result_context = task.model_dump(mode="json")
                row.upstream_request_id = receipt.request_id
                row.receipt_ids = dict(observation.receipt_ids) or None
                row.usage = observation.usage
                row.updated_at = utcnow()
            finished = True

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
                    result_context = (
                        event["content"].manifest.model_dump(mode="json")
                        if isinstance(event["content"], ImageBatch)
                        else None
                    )
                    if isinstance(event["content"], ImageSources):
                        from keystone.platform.model_access.services.image_results import (
                            seal_sources,
                        )

                        result_context = seal_sources(
                            self.clients, selected, identifier, event["content"]
                        ).model_dump(mode="json")
                    if isinstance(event["content"], ImageTaskReceipt):
                        await accepted_task(event["content"])
                    else:
                        await finish("succeeded", result_context=result_context)
                    yield {
                        "type": "done",
                        "invocation_id": str(identifier),
                        "content": event["content"],
                    }
        except BaseException as error:
            failed(error, "inference")
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

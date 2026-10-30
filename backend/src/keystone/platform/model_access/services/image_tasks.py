"""Known native image tasks only: original receipts, bounded queries and result evidence."""

import hashlib
import math
from datetime import timedelta

from sqlalchemy import select

from keystone.core.errors import ServiceError
from keystone.core.time import utcnow
from keystone.platform.model_access.adapters.channels import channel_adapter
from keystone.platform.model_access.adapters.inference.pricing import (
    PROFILES,
    normalize_pricing_facts,
)
from keystone.platform.model_access.models import Channel, ModelEntry, ModelInvocation
from keystone.platform.model_access.schemas.errors import QueryLater
from keystone.platform.model_access.schemas.images import (
    ImageBatch,
    ImageManifest,
    ImageOutput,
    ImagePending,
    ImageTask,
    ImageTaskReceipt,
    ImageTaskRequest,
)
from keystone.platform.model_access.schemas.invocations import InvocationContext
from keystone.platform.model_access.schemas.pricing import PricingFacts
from keystone.platform.model_access.services.cost_policy import initialize_check
from keystone.platform.model_access.services.pricing import apply_estimate
from keystone.platform.model_access.services.queries import locked
from keystone.platform.model_access.services.runtime import ModelRuntime


def read_evidence(row):
    try:
        contract = ImageManifest if row.result_context.get("kind") == "images" else ImageTaskReceipt
        value = contract.model_validate(row.result_context)
        if isinstance(value, ImageManifest) and value.task is None:
            raise ValueError()
        return value
    except (AttributeError, ValueError, TypeError):
        raise ServiceError(409, "model_result_unavailable", "原任务证据不可用，禁止重发") from None


def original_task(evidence):
    return evidence.task if isinstance(evidence, ImageManifest) else evidence


def replace_task(row, evidence, task):
    value = (
        evidence.model_copy(update={"task": task}) if isinstance(evidence, ImageManifest) else task
    )
    row.result_context = value.model_dump(mode="json")
    row.updated_at = utcnow()


def interrupted(row, code):
    if row.status == "running":
        row.status, row.error_code = "unknown", code
        row.finished_at = row.updated_at = utcnow()
        if row.cost_deadline_at is None:
            initialize_check(row, row.finished_at)
        apply_estimate(row)


async def abandon_in_transaction(session, run_id):
    """Run terminalization closes local observation, never cancels a vendor task."""
    rows = await session.scalars(
        select(ModelInvocation)
        .where(
            ModelInvocation.flow_context["flow_run_id"].astext == str(run_id),
            ModelInvocation.result_context["kind"].astext == "task",
            ModelInvocation.status == "running",
        )
        .order_by(ModelInvocation.id)
        .with_for_update()
    )
    for row in rows:
        interrupted(row, "model_interrupted")


class ImageTaskService:
    def __init__(self, service, *, runtime=None):
        self.identity, self.clients = service.identity, service.clients
        self.runtime = runtime if runtime is not None else ModelRuntime(service)

    @staticmethod
    def require_original(row, selected, context):
        try:
            original = (
                InvocationContext.model_validate(row.flow_context).authority() if row else None
            )
        except ValueError:
            original = None
        adapter = channel_adapter(selected.provider, selected.api_base)
        if (
            row is None
            or row.operation != "image.generate"
            or selected.delivery != "async"
            or adapter is None
            or selected.api_base != adapter.api_base
            or not adapter.supports(
                selected.operation, selected.delivery, model=selected.provider_model
            )
            or original != context.authority()
            or row.model_entry_id != selected.model_entry_id
            or row.channel_id != selected.channel_id
            or row.provider_model != selected.provider_model
        ):
            raise ServiceError(409, "model_result_unavailable", "原图片任务与本次运行不匹配")

    async def evidence(self, identifier, selected, context):
        async with self.identity.access("model:invoke") as (session, _):
            row = await session.get(ModelInvocation, identifier)
            self.require_original(row, selected, context)
            return read_evidence(row)

    async def submit(self, data, *, selected, identifier, context, worker_identity):
        data = ImageTaskRequest.model_validate(data.model_dump(mode="json"))
        self.clients.inference.task_protocol(selected)
        if selected.operation != "image.generate" or selected.delivery != "async":
            raise ServiceError(422, "model_invalid_request", "异步图片角色操作不匹配")
        async with self.identity.access("model:invoke") as (session, _):
            row = await session.get(ModelInvocation, identifier)
            if row is not None:
                self.require_original(row, selected, context)
                evidence = read_evidence(row)
                if evidence.request_digest != self.runtime.image_digest(data, "image.generate"):
                    raise ServiceError(409, "model_result_unavailable", "原图片任务请求不匹配")
                return ImageTask(invocation_id=identifier)

        async def send(sdk):
            task_id = await self.clients.inference.submit_task(sdk, selected, data)
            now = utcnow()
            return ImageTaskReceipt(
                task_id=task_id,
                count=data.n,
                submitted_at=now,
                deadline_at=now + timedelta(seconds=300),
                next_query_at=now,
                request_digest=self.runtime.image_digest(data, "image.generate"),
            )

        try:
            await self.runtime.invoke_sdk(
                selection=selected,
                invocation_id=identifier,
                sdk_request=send,
                flow_context=context,
                worker_identity=worker_identity,
            )
        except BaseException:
            from keystone.platform.model_access.services.runtime import settle

            await settle(self.abandon(identifier, selected, context))
            raise
        return ImageTask(invocation_id=identifier)

    async def abandon(self, identifier, selected, context):
        async with self.identity.database.transaction() as session:
            row = await session.get(ModelInvocation, identifier, with_for_update=True)
            if row is None:
                return
            self.require_original(row, selected, context)
            if row.result_context is not None:
                interrupted(row, "model_interrupted")

    async def credentials(self, selected):
        async with self.identity.access("model:invoke") as (session, _):
            return await self._credentials(session, selected)

    async def _credentials(self, session, selected):
        entry = await locked(session, ModelEntry, selected.model_entry_id)
        channel = await locked(session, Channel, selected.channel_id)
        if (
            not entry.is_active
            or not channel.is_active
            or entry.channel_id != selected.channel_id
            or entry.provider_model != selected.provider_model
            or channel.provider != selected.provider
            or channel.api_base != selected.api_base
            or selected.operation not in entry.operations
        ):
            raise ServiceError(409, "model_configuration_changed", "原模型任务配置已改变")
        self.clients.inference.check_operation(
            channel.provider,
            selected.operation,
            selected.delivery,
            model=selected.provider_model,
        )
        secret = self.clients.credentials.decrypt(
            channel.api_key_encrypted, channel.id, "inference"
        )
        return secret, self.clients.credentials.scope(selected.api_base, secret)

    async def claim_query(self, selected, secret, scope):
        async with self.identity.access("model:invoke") as (session, _):
            current, current_scope = await self._credentials(session, selected)
            if current != secret or current_scope != scope:
                raise ServiceError(409, "model_configuration_changed", "原任务凭据已改变")
            return await self.runtime.dispatch.claim_in_transaction(session, scope)

    async def reserve_query(self, identifier, selected, context):
        expired = False
        async with self.identity.access("model:invoke") as (session, _):
            row = await locked(session, ModelInvocation, identifier)
            self.require_original(row, selected, context)
            evidence = read_evidence(row)
            task = original_task(evidence)
            now = utcnow()
            if row.status == "failed":
                raise ServiceError(502, "model_task_failed", "原图片任务已明确失败")
            if now >= task.deadline_at or task.queries >= 60:
                interrupted(row, "model_task_timeout")
                expired = True
            elif now < task.next_query_at:
                return None, ImagePending(
                    invocation_id=identifier,
                    retry_after_seconds=max(
                        1, min(60, math.ceil((task.next_query_at - now).total_seconds()))
                    ),
                )
            else:
                task = task.model_copy(
                    update={
                        "queries": task.queries + 1,
                        "next_query_at": min(task.deadline_at, now + timedelta(seconds=5)),
                    }
                )
                replace_task(row, evidence, task)
        if expired:
            raise ServiceError(504, "model_task_timeout", "原图片任务超出有界查询预算")
        return task, None

    async def delay(self, identifier, selected, context, seconds):
        async with self.identity.database.transaction() as session:
            row = await locked(session, ModelInvocation, identifier)
            self.require_original(row, selected, context)
            evidence = read_evidence(row)
            task, now = original_task(evidence), utcnow()
            due = min(task.deadline_at, max(task.next_query_at, now + timedelta(seconds=seconds)))
            replace_task(row, evidence, task.model_copy(update={"next_query_at": due}))
            return ImagePending(
                invocation_id=identifier,
                retry_after_seconds=max(1, min(60, math.ceil((due - now).total_seconds()))),
            )

    async def known_status(
        self, identifier, selected, context, status, *, manifest=None, generated=None
    ):
        async with self.identity.database.transaction() as session:
            row = await locked(session, ModelInvocation, identifier)
            self.require_original(row, selected, context)
            evidence = read_evidence(row)
            if row.status in {"failed", "succeeded"} and row.status != status:
                raise ServiceError(409, "model_result_unavailable", "原图片任务结果出现冲突")
            if manifest is not None:
                if isinstance(evidence, ImageManifest) and evidence.outputs != manifest.outputs:
                    raise ServiceError(409, "model_result_unavailable", "原图片任务产物已改变")
                row.result_context = manifest.model_dump(mode="json")
            row.status, row.error_code = status, "model_task_failed" if status == "failed" else None
            row.finished_at = row.finished_at or utcnow()
            row.updated_at = utcnow()
            if row.cost_deadline_at is None:
                initialize_check(row, row.finished_at)
            if generated is not None:
                facts = normalize_pricing_facts(
                    PROFILES["kling_images.v1"],
                    {},
                    complete=True,
                    success=True,
                    generated=generated,
                )
                try:
                    prior = (
                        PricingFacts.model_validate(row.estimate_facts)
                        if row.estimate_facts
                        else None
                    )
                    previous = prior.values.get("usage.generated_images") if prior else None
                    if (
                        prior
                        and prior.error
                        or previous is not None
                        and previous.value != generated
                    ):
                        facts.error = "meter_conflict"
                except ValueError:
                    facts.error = "meter_conflict"
                apply_estimate(row, facts)
            else:
                apply_estimate(row)

    async def poll(self, identifier, *, selected, context):
        task, pending = await self.reserve_query(identifier, selected, context)
        if pending:
            return pending
        try:
            secret, scope = await self.credentials(selected)
            async with self.clients.coordination.inference(scope) as permit:
                delay = await self.claim_query(selected, secret, scope)
                if delay:
                    return await self.delay(identifier, selected, context, math.ceil(delay))
                await permit()
                state, urls = await self.clients.inference.query_task(selected, task, secret)
                if state in {"submitted", "processing"}:
                    return await self.delay(identifier, selected, context, 5)
                if state == "failed":
                    await self.known_status(identifier, selected, context, "failed")
                    raise ServiceError(502, "model_task_failed", "原图片任务已明确失败")
                await self.known_status(
                    identifier, selected, context, "succeeded", generated=len(urls)
                )
                outputs, contents = [], []
                for index, url in enumerate(urls):
                    await self.credentials(selected)
                    await permit()
                    content = await self.clients.inference.download_task_output(selected, url)
                    content_type = await self.clients.inference.verify_output_image(content)
                    contents.append(content)
                    outputs.append(
                        ImageOutput(
                            artifact_key=f"model.{identifier}.{index}",
                            checksum_sha256=hashlib.sha256(content).hexdigest(),
                            size_bytes=len(content),
                            content_type=content_type,
                        )
                    )
                manifest = ImageManifest(
                    request_digest=task.request_digest,
                    operation="image.generate",
                    outputs=outputs,
                    task=task,
                )
                await self.known_status(
                    identifier, selected, context, "succeeded", manifest=manifest
                )
                return ImageBatch(manifest, tuple(contents))
        except QueryLater as error:
            if error.rate_limited:
                await self.runtime.dispatch.cooldown(scope, error.cooldown_seconds)
            return await self.delay(identifier, selected, context, error.seconds)
        except (AttributeError, ValueError, TypeError, RecursionError):
            await self.abandon(identifier, selected, context)
            raise ServiceError(502, "model_invalid_output", "原任务返回了无效结果") from None
        except BaseException:
            # Persist only this known task's local interruption; never cancel/resubmit it.
            from keystone.platform.model_access.services.runtime import settle

            await settle(self.abandon(identifier, selected, context))
            raise

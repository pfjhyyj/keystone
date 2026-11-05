"""Only trusted runtime handles live here. DBOS checkpoints receive IDs and small values."""

import asyncio
import logging
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
from uuid import UUID

from keystone.core.errors import ServiceError
from keystone.platform.flow.schemas.artifacts import ImageResult
from keystone.platform.flow.services.identity import ExecutionIdentity
from keystone.platform.flow.services.resources import FlowResourceService

_bound_runtime = None
_execution = ContextVar("flow_execution_scope", default=None)
_workspaces = ContextVar("flow_workspaces", default=())
_model_wait = ContextVar("flow_model_wait", default=None)


def _bind(runtime):
    global _bound_runtime
    _bound_runtime = runtime


def _runtime():
    if _bound_runtime is None:
        raise RuntimeError("Flow host is not bound")
    return _bound_runtime


def _identity(run_id):
    return ExecutionIdentity(_runtime(), run_id)


@asynccontextmanager
async def _execution_scope(run_id, *, permissions=()):
    """Keep this Run's original authority live across a long operation, outside SQL I/O.

    Extra permissions are trusted business declarations. Yield a fresh check for
    critical boundaries, and recheck on normal exit; never replace business failures.
    Nested scopes for this Run retain the outer permission requirements.
    """
    if isinstance(permissions, str):
        raise ValueError("Permissions must be a sequence of permission keys")
    permissions = tuple(permissions)
    if any(not isinstance(p, str) or not p for p in permissions):
        raise ValueError("Permissions must be a sequence of permission keys")
    run_id = UUID(str(run_id))
    outer = _execution.get()
    if outer and outer[0] != run_id:
        raise ServiceError(404, "flow_not_found", "不能在其他运行的作用域中执行")
    required = frozenset(permissions) | (outer[1] if outer else frozenset())
    context = _identity(run_id)
    parent = asyncio.current_task()
    reason = None

    async def check():
        async with context.access() as (_, auth):
            for permission in sorted(required):
                auth.require(permission)

    await check()
    token = _execution.set((run_id, required))
    waiting = _model_wait.get()
    if waiting is None or waiting[0] is not parent:
        waiting = [parent, 0]
    waiting_token = _model_wait.set(waiting)

    async def monitor():
        nonlocal reason
        while True:
            await asyncio.sleep(1)
            try:
                # An in-flight model retains the right to save its receipt;
                # each new physical send still authorizes before registration.
                if not waiting[1]:
                    await check()
            except ServiceError as error:
                if error.status in {401, 403, 404} or error.code in {
                    "flow_cancelled",
                    "flow_not_found",
                    "execution_not_authorized",
                    "execution_version_unavailable",
                }:
                    reason = error
                    parent.cancel()
                    return
                logging.getLogger(__name__).warning("execution_monitor_retry type=ServiceError")
            except Exception as error:
                # A dependency outage must not silently terminate the cancellation monitor.
                logging.getLogger(__name__).warning(
                    "execution_monitor_retry type=%s", type(error).__name__
                )

    task = asyncio.create_task(monitor())
    failed = False
    try:
        yield check
        await check()
    except asyncio.CancelledError:
        if reason is not None:
            raise reason from None
        raise
    except BaseException:
        failed = True
        raise
    finally:
        _execution.reset(token)
        _model_wait.reset(waiting_token)
        task.cancel()
        draining = asyncio.gather(task, return_exceptions=True)
        interrupted = False
        while not draining.done():
            try:
                await asyncio.shield(draining)
            except asyncio.CancelledError:
                interrupted = True
        if interrupted and not failed:
            raise reason or asyncio.CancelledError()


@asynccontextmanager
async def _model_operation_scope(run_id):
    async with _execution_scope(run_id, permissions=("model:invoke",)):
        waiting = _model_wait.get()
        waiting[1] += 1
        try:
            yield
        finally:
            waiting[1] -= 1


@asynccontextmanager
async def _workspace(run_id, *, max_bytes):
    resource = _resources(run_id)
    files = resource.files
    with files.transfer() as transfer:
        async with _execution_scope(run_id):
            async with transfer.workspace(max_bytes) as path:
                state = [UUID(str(run_id)), asyncio.current_task(), path, max_bytes, True]
                token = _workspaces.set((*_workspaces.get(), state))
                try:
                    yield path
                finally:
                    state[4] = False
                    _workspaces.reset(token)


async def _run_process(run_id, arguments, *, permissions=(), **options):
    """Run a trusted argv within fresh Run authorization; None uses the active scope."""
    from keystone.core.processes import run_process as execute

    if run_id is None:
        current = _execution.get()
        if current is None:
            raise RuntimeError("A process requires an explicit Run or active execution scope")
        run_id = current[0]
    output = options.get("output")
    if output is not None:
        maximum = options.get("output_limit")
        target = Path(output)
        if (
            type(maximum) is not int
            or maximum < 0
            or target.is_symlink()
            or not any(
                item[0] == UUID(str(run_id))
                and item[1] is asyncio.current_task()
                and item[4]
                and target.resolve() != item[2].resolve()
                and target.resolve().is_relative_to(item[2].resolve())
                and not any(
                    parent.is_symlink() or parent.is_junction()
                    for parent in target.parents
                    if parent != item[2] and parent.is_relative_to(item[2])
                )
                and maximum <= item[3]
                for item in _workspaces.get()
            )
        ):
            raise ServiceError(
                422, "flow_process_output_denied", "进程输出需要当前托管工作区和明确额度"
            )
    async with _execution_scope(run_id, permissions=permissions):
        return await execute(arguments, **options)


def _resources(run_id):
    return FlowResourceService(_identity(run_id), _runtime().storage)


async def _inputs(run_id):
    return await _resources(run_id).inputs()


async def _read_file(run_id, identifier, *, max_bytes):
    async with _execution_scope(run_id):
        return await _resources(run_id).read_file(identifier, max_bytes=max_bytes)


async def _upload(run_id, key, name, content, *, content_type, storage_key=None):
    async with _execution_scope(run_id):
        return await _resources(run_id).upload(
            key,
            name,
            content,
            content_type=content_type,
            storage_key=storage_key,
        )


async def _artifact(run_id, key, *, max_bytes=None):
    async with _execution_scope(run_id):
        return await _resources(run_id).artifact(key, max_bytes=max_bytes)


@asynccontextmanager
async def _materialize_file(run_id, identifier):
    # Preserve slot -> live execution scope -> authorized reader/workspace nesting.
    resource = _resources(run_id)
    await resource.require_file(identifier)
    files = resource.files
    with files.transfer() as transfer:
        async with (
            _execution_scope(run_id),
            transfer.materialize_file(UUID(str(identifier))) as path,
        ):
            yield path


async def _upload_file(run_id, key, path, *, name, content_type, storage_key=None):
    resource = _resources(run_id)
    files = resource.files
    with files.transfer() as transfer:
        async with _execution_scope(run_id):
            return await resource.upload_file(
                key,
                path,
                name=name,
                content_type=content_type,
                storage_key=storage_key,
                transfer=transfer,
            )


class FlowContext:
    """Short-lived capabilities for one Run. Never serialize this object into a Step."""

    __slots__ = ("_run_id",)

    def __init__(self, run_id):
        self._run_id = UUID(str(run_id))

    @property
    def run_id(self):
        return self._run_id

    async def inputs(self):
        return await _inputs(self.run_id)

    async def check(self, *, permissions=()):
        if isinstance(permissions, str):
            raise ValueError("Permissions must be a sequence")
        async with _identity(self.run_id).access() as (_, auth):
            for permission in permissions:
                auth.require(permission)

    def execution_scope(self, *, permissions=()):
        return _execution_scope(self.run_id, permissions=permissions)

    def workspace(self, *, max_bytes):
        return _workspace(self.run_id, max_bytes=max_bytes)

    async def read_file(self, file_id, *, max_bytes):
        return await _read_file(self.run_id, file_id, max_bytes=max_bytes)

    def materialize_file(self, file_id):
        return _materialize_file(self.run_id, file_id)

    async def upload(self, key, name, content, *, content_type):
        return UUID(await _upload(self.run_id, key, name, content, content_type=content_type))

    async def upload_file(self, key, path, *, name, content_type):
        return UUID(
            await _upload_file(self.run_id, key, path, name=name, content_type=content_type)
        )

    async def artifacts(self, keys, *, max_bytes=None):
        values = await _resources(self.run_id).artifacts(keys, max_bytes=max_bytes)
        return {key: UUID(value) if value else None for key, value in values.items()}

    @asynccontextmanager
    async def local_file(self, identifier):
        async with _execution_scope(self.run_id):
            async with _resources(self.run_id).local_file(identifier) as local:
                yield local

    async def artifact(self, key, *, max_bytes=None):
        result = await _artifact(self.run_id, key, max_bytes=max_bytes)
        return UUID(result) if result else None

    async def run_process(self, argv, **bounded_options):
        return await _run_process(self.run_id, argv, **bounded_options)

    def _invocation_context(self, role, *, node_key, agent_key=None, agent_call_id=None):
        from dbos import DBOS

        from keystone.platform.model_access.schemas.invocations import InvocationContext

        if DBOS.workflow_id is None or DBOS.step_id is None:
            raise RuntimeError("Model correlation requires a durable DBOS step")
        return InvocationContext(
            flow_run_id=self.run_id,
            node_key=node_key,
            model_role=role,
            agent_key=agent_key,
            agent_call_id=agent_call_id,
            workflow_id=DBOS.workflow_id,
            step_id=DBOS.step_id,
        )

    async def _model(self, role, *, operation="text.generate", delivery="sync"):
        from uuid import uuid5

        from dbos import DBOS

        from keystone.platform.model_access.schemas.invocations import Selection
        from keystone.platform.model_access.services.entries import ModelEntryService
        from keystone.platform.model_access.services.runtime import ModelRuntime

        if DBOS.workflow_id is None or DBOS.step_id is None:
            raise RuntimeError("Model calls require a durable DBOS step")
        binding = await _resources(self.run_id).model_binding(role)
        selected = Selection.model_validate(binding["selection"])
        if selected.operation != operation or selected.delivery != delivery:
            from keystone.core.errors import ServiceError

            raise ServiceError(409, "model_operation_unavailable", "模型角色操作不匹配")
        identifier = uuid5(self.run_id, f"model:{DBOS.workflow_id}:{DBOS.step_id}")
        service = ModelEntryService(_identity(self.run_id), _runtime().models)

        return (
            ModelRuntime(
                service,
                operation_scope=lambda: _model_operation_scope(self.run_id),
            ),
            selected,
            identifier,
        )

    async def _text(self, role, request, *, node_key, operation="text.generate"):
        model, selected, identifier = await self._model(role, operation=operation)
        result = await model.invoke_text(
            request,
            selection=selected,
            invocation_id=identifier,
            context=self._invocation_context(role, node_key=node_key),
            worker_identity=_runtime().worker_identity,
        )
        return identifier, result["content"]

    async def invoke_text(self, role, request, *, node_key):
        from keystone.platform.model_access.schemas.invocations import (
            TextRequest,
            TextResult,
        )

        if isinstance(request, TextRequest):
            request = request.model_dump(mode="json")
        request = TextRequest.model_validate(request)
        identifier, content = await self._text(role, request, node_key=node_key)
        return TextResult(invocation_id=identifier, content=content)

    async def invoke_embedding(self, role, request, *, node_key):
        from keystone.platform.model_access.schemas.embeddings import (
            EmbeddingRequest,
            EmbeddingResult,
        )

        if isinstance(request, EmbeddingRequest):
            request = request.model_dump(mode="json")
        request = EmbeddingRequest.model_validate(request)
        model, selected, identifier = await self._model(role, operation="text.embed")
        vectors = await model.invoke_embedding(
            request,
            selection=selected,
            invocation_id=identifier,
            context=self._invocation_context(role, node_key=node_key),
            worker_identity=_runtime().worker_identity,
        )
        # The completed attempt keeps its receipts; cancellation/revocation must
        # still prevent exposing these vectors to a new business publication.
        await self.check(permissions=("model:invoke",))
        return EmbeddingResult(invocation_id=identifier, vectors=vectors)

    async def submit_image(self, role, request, *, node_key):
        from keystone.platform.model_access.schemas.images import ImageTaskRequest
        from keystone.platform.model_access.services.image_tasks import ImageTaskService
        from keystone.platform.model_access.services.runtime import settle

        if isinstance(request, ImageTaskRequest):
            request = request.model_dump(mode="json")
        request = ImageTaskRequest.model_validate(request)
        handle, tasks, selected, context = None, None, None, None
        try:
            async with self.execution_scope(permissions=("model:invoke",)):
                model, selected, identifier = await self._model(
                    role,
                    operation="image.generate",
                    delivery="async",
                )
                context = self._invocation_context(role, node_key=node_key)
                tasks = ImageTaskService(model.service, runtime=model)
                handle = await tasks.submit(
                    request,
                    selected=selected,
                    identifier=identifier,
                    context=context,
                    worker_identity=_runtime().worker_identity,
                )
                return handle
        except BaseException:
            if handle is not None:
                await settle(tasks.abandon(handle.invocation_id, selected, context))
            raise

    async def image_result(self, role, invocation_id, *, node_key):
        from keystone.platform.model_access.schemas.images import (
            ImageManifest,
            ImagePending,
        )
        from keystone.platform.model_access.services.image_tasks import ImageTaskService
        from keystone.platform.model_access.services.runtime import settle

        identifier = UUID(str(invocation_id))
        tasks, selected, context = None, None, None
        try:
            async with self.execution_scope(permissions=("model:invoke",)):
                model, selected, _ = await self._model(
                    role, operation="image.generate", delivery="async"
                )
                context = self._invocation_context(role, node_key=node_key)
                tasks = ImageTaskService(model.service, runtime=model)
                evidence = await tasks.evidence(identifier, selected, context)
                if isinstance(evidence, ImageManifest):
                    restored = await self._restore_images(evidence)
                    if restored is not None:
                        return ImageResult(invocation_id=identifier, files=restored)
                result = await tasks.poll(identifier, selected=selected, context=context)
                if isinstance(result, ImagePending):
                    return result
                return ImageResult(
                    invocation_id=identifier, files=await self._publish_images(result)
                )
        except BaseException:
            if tasks is not None:
                await settle(tasks.abandon(identifier, selected, context))
            raise

    async def _image_inputs(self, references, *, max_total=None):
        from keystone.platform.model_access.schemas.images import (
            MAX_IMAGE_BYTES,
            MAX_INPUT_IMAGE_BYTES,
        )

        images, total = [], 0
        for image in references:
            content_type, content = await _resources(self.run_id).image_content(
                image.file_id, max_bytes=MAX_IMAGE_BYTES
            )
            total += len(content)
            if total > (MAX_INPUT_IMAGE_BYTES if max_total is None else max_total):
                raise ServiceError(413, "model_input_too_large", "图片累计大小超过上限")
            images.append((content_type, content, image.detail))
        return images

    async def _publish_images(self, batch):
        files = []
        for output, content in zip(batch.manifest.outputs, batch.contents, strict=True):
            extension = output.content_type.removeprefix("image/")
            file_id = await self.upload(
                output.artifact_key,
                f"{output.artifact_key}.{extension}",
                content,
                content_type=output.content_type,
            )
            files.append({"file_id": file_id})
        return files

    async def _restore_images(self, manifest):
        import hashlib

        from keystone.platform.model_access.schemas.images import MAX_OUTPUT_IMAGE_BYTES

        files = []
        for output in manifest.outputs:
            file_id = await self.artifact(output.artifact_key, max_bytes=MAX_OUTPUT_IMAGE_BYTES)
            if file_id is None:
                return None
            content_type, content = await _resources(self.run_id).image_content(
                file_id,
                max_bytes=MAX_OUTPUT_IMAGE_BYTES,
            )
            if (
                len(content) != output.size_bytes
                or content_type != output.content_type
                or hashlib.sha256(content).hexdigest() != output.checksum_sha256
            ):
                raise ServiceError(409, "model_result_unavailable", "原图片产物证据不匹配")
            files.append({"file_id": file_id})
        return files

    async def invoke_image(self, role, request, *, node_key):
        from keystone.platform.model_access.schemas.images import ImageGenerateRequest

        if isinstance(request, ImageGenerateRequest):
            request = request.model_dump(mode="json")
        request = ImageGenerateRequest.model_validate(request)
        return await self._image_result(
            role, request, node_key=node_key, operation="image.generate"
        )

    async def edit_image(self, role, request, *, node_key):
        from keystone.platform.model_access.schemas.images import ImageEditRequest

        if isinstance(request, ImageEditRequest):
            request = request.model_dump(mode="json")
        request = ImageEditRequest.model_validate(request)
        return await self._image_result(role, request, node_key=node_key, operation="image.edit")

    async def _image_result(self, role, request, *, node_key, operation):
        from keystone.platform.model_access.schemas.images import ImageManifest

        permissions = ("model:invoke",)
        async with self.execution_scope(permissions=permissions) as check:
            model, selected, identifier = await self._model(role, operation=operation)
            context = self._invocation_context(role, node_key=node_key)
            manifest = await model.image_manifest(
                identifier,
                data=request,
                selection=selected,
                context=context,
            )
            files = []
            if manifest is not None:
                files = (
                    await self._restore_images(manifest)
                    if isinstance(manifest, ImageManifest)
                    else None
                )
                if files is None:
                    if isinstance(manifest, ImageManifest) and manifest.download is None:
                        raise ServiceError(
                            409, "model_result_unavailable", "原图片产物未完整保存，禁止重发"
                        )
                    batch = await model.recover_image(
                        identifier, selection=selected, context=context, evidence=manifest
                    )
                    files = await self._publish_images(batch)
            else:
                max_images, max_total = model.image_limits(selected)
                references = getattr(request, "images", ())
                if len(references) > max_images:
                    raise ServiceError(422, "model_invalid_request", "图片数量超过协议上限")
                images = await self._image_inputs(references, max_total=max_total)
                await check()
                batch = await model.invoke_image(
                    request,
                    images=images,
                    selection=selected,
                    invocation_id=identifier,
                    context=context,
                    worker_identity=_runtime().worker_identity,
                )
                files = await self._publish_images(batch)
            await check()
            return ImageResult(invocation_id=identifier, files=files)

    async def invoke_vision(self, role, request, *, node_key):
        from keystone.platform.model_access.schemas.images import VisionRequest
        from keystone.platform.model_access.schemas.invocations import TextResult

        if isinstance(request, VisionRequest):
            request = request.model_dump(mode="json")
        request = VisionRequest.model_validate(request)
        async with self.execution_scope(permissions=("model:invoke",)) as check:
            model, selected, identifier = await self._model(role, operation="image.understand")
            images = await self._image_inputs(request.images)
            await check()
            content = await model.invoke_vision(
                request,
                images=images,
                selection=selected,
                invocation_id=identifier,
                context=self._invocation_context(role, node_key=node_key),
                worker_identity=_runtime().worker_identity,
            )
            return TextResult(invocation_id=identifier, content=content)

    async def invoke_structured(self, role, request, *, node_key):
        from keystone.platform.model_access.schemas.structured import (
            StructuredRequest,
            StructuredResult,
            parse_structured,
        )

        if isinstance(request, StructuredRequest):
            request = request.model_dump(mode="json")
        request = StructuredRequest.model_validate(request)
        identifier, content = await self._text(
            role, request, node_key=node_key, operation="text.structured"
        )
        return StructuredResult(
            invocation_id=identifier, content=parse_structured(content, request.output)
        )


def flow_context(run_id):
    return FlowContext(run_id)

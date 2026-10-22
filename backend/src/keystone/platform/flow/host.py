"""Only trusted runtime handles live here. DBOS checkpoints receive IDs and small values."""

import asyncio
import hashlib
import logging
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
from threading import Event
from uuid import UUID

from keystone.core.errors import ServiceError
from keystone.platform.files.schemas import Upload
from keystone.platform.files.services.files import FileService
from keystone.platform.files.storage import blocking, opened
from keystone.platform.files.transfers import CHUNK_BYTES, checksum
from keystone.platform.flow.identity import ExecutionIdentity
from keystone.platform.flow.models import FlowRun
from keystone.platform.flow.services.artifacts import FlowArtifactService, artifact_location

_runtime = None
_execution = ContextVar("flow_execution_scope", default=None)


def bind(runtime):
    global _runtime
    _runtime = runtime


def runtime():
    if _runtime is None:
        raise RuntimeError("Flow host is not bound")
    return _runtime


def identity(run_id):
    return ExecutionIdentity(runtime(), run_id)


@asynccontextmanager
async def execution_scope(run_id, *, permissions=()):
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
    context = identity(run_id)
    parent = asyncio.current_task()
    reason = None

    async def check():
        async with context.access() as (_, auth):
            for permission in sorted(required):
                auth.require(permission)

    await check()
    token = _execution.set((run_id, required))

    async def monitor():
        nonlocal reason
        while True:
            await asyncio.sleep(1)
            try:
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


async def run_process(run_id, arguments, *, permissions=(), **options):
    """Run a trusted argv within fresh Run authorization; None uses the active scope."""
    from keystone.core.processes import run_process as execute

    if run_id is None:
        current = _execution.get()
        if current is None:
            raise RuntimeError("A process requires an explicit Run or active execution scope")
        run_id = current[0]
    async with execution_scope(run_id, permissions=permissions):
        return await execute(arguments, **options)


async def inputs(run_id):
    async with identity(run_id).access() as (session, _):
        return (await session.get(FlowRun, UUID(run_id))).input


async def read_file(run_id, identifier, *, max_bytes):
    _, content = await FileService(identity(run_id), runtime().storage).content(
        UUID(identifier), max_bytes=max_bytes
    )
    return content


async def upload(run_id, key, name, content, *, content_type, storage_key=None):
    owner, slot = artifact_location(run_id, key)
    request_key = f"{owner}:{key}"
    result = await FileService(identity(run_id), runtime().storage).upload_referenced(
        Upload(
            storage_key=storage_key,
            name=name,
            content_type=content_type,
            expected_size=len(content),
            request_key=request_key,
            checksum_sha256=hashlib.sha256(content).hexdigest(),
        ),
        content,
        owner=owner,
        slot=slot,
    )
    return str(result["id"])


async def artifact(run_id, key, *, max_bytes=None):
    """Find this execution's verified artifact without exposing file-reference tables."""
    return await FlowArtifactService(identity(run_id), runtime().storage).find(
        run_id, key, max_bytes=max_bytes
    )


@asynccontextmanager
async def materialize_file(run_id, identifier):
    """Authorized bounded local working copy. Keep the context inside a durable step.

    Paths/handles are ephemeral and must never be returned as DBOS checkpoints.
    Cancellation closes the reader and deletes scratch before releasing its slot.
    """
    service = FileService(identity(run_id), runtime().storage)
    transfers = runtime().storage.transfers
    with transfers.slot() as number:
        async with (
            execution_scope(run_id),
            service.open_content(
                UUID(identifier), _transfer_slot=number, max_bytes=transfers.limit
            ) as reader,
        ):
            async with transfers.workspace(reader.size, _slot=number) as workspace:
                path = workspace.path
                digest = hashlib.sha256()
                async with opened(path.open, "xb") as target:
                    async for chunk in reader.chunks():
                        await blocking(workspace.capacity.write, target, chunk)
                        digest.update(chunk)
                if digest.hexdigest() != reader.checksum:
                    raise ServiceError(503, "file_corrupt", "文件内容校验失败")
                yield path


async def upload_file(run_id, key, path, *, name, content_type, storage_key=None):
    """Stream a trusted business-produced local file; retain the artifact before receipt."""
    owner, slot = artifact_location(run_id, key)
    service = FileService(identity(run_id), runtime().storage)
    transfers = runtime().storage.transfers
    with transfers.slot() as number:
        async with execution_scope(run_id), opened(Path(path).open, "rb") as source:
            size = await blocking(source.seek, 0, 2)
            await service.prepare_upload(size, storage_key)
            if size > transfers.limit:
                raise ServiceError(413, "file_temporary_limit", "文件超过临时空间配额")
            await blocking(source.seek, 0)

            cancelled = Event()
            try:
                digest = await blocking(checksum, source, size, cancelled, on_cancel=cancelled.set)
            except ValueError:
                raise ServiceError(422, "file_size_mismatch", "上传文件在校验时发生变化") from None
            await blocking(source.seek, 0)

            async def chunks():
                while chunk := await blocking(source.read, CHUNK_BYTES):
                    yield chunk

            result = await service.upload_stream(
                Upload(
                    storage_key=storage_key,
                    name=name,
                    content_type=content_type,
                    expected_size=size,
                    checksum_sha256=digest,
                    request_key=f"{owner}:{key}",
                ),
                chunks(),
                owner=owner,
                slot=slot,
                _transfer_slot=number,
            )
            return str(result["id"])

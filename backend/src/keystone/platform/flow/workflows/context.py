"""Only trusted runtime handles live here. DBOS checkpoints receive IDs and small values."""

import asyncio
import logging
from contextlib import asynccontextmanager
from contextvars import ContextVar
from uuid import UUID

from keystone.core.errors import ServiceError
from keystone.platform.flow.services.identity import ExecutionIdentity
from keystone.platform.flow.services.resources import FlowResourceService

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


def resources(run_id):
    return FlowResourceService(identity(run_id), runtime().storage)


async def inputs(run_id):
    return await resources(run_id).inputs()


async def read_file(run_id, identifier, *, max_bytes):
    return await resources(run_id).read_file(identifier, max_bytes=max_bytes)


async def upload(run_id, key, name, content, *, content_type, storage_key=None):
    return await resources(run_id).upload(
        key,
        name,
        content,
        content_type=content_type,
        storage_key=storage_key,
    )


async def artifact(run_id, key, *, max_bytes=None):
    return await resources(run_id).artifact(key, max_bytes=max_bytes)


@asynccontextmanager
async def materialize_file(run_id, identifier):
    # Preserve slot -> live execution scope -> authorized reader/workspace nesting.
    resource = resources(run_id)
    with resource.files.transfer() as transfer:
        async with (
            execution_scope(run_id),
            transfer.materialize_file(UUID(identifier)) as path,
        ):
            yield path


async def upload_file(run_id, key, path, *, name, content_type, storage_key=None):
    resource = resources(run_id)
    with resource.files.transfer() as transfer:
        async with execution_scope(run_id):
            return await resource.upload_file(
                key,
                path,
                name=name,
                content_type=content_type,
                storage_key=storage_key,
                transfer=transfer,
            )

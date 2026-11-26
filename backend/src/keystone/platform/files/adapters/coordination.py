"""File I/O admission; PostgreSQL settlements stay on the lock-owning connection."""

from contextlib import asynccontextmanager, nullcontext

from keystone.core.advisory import lock_key, session_lease
from keystone.core.errors import ServiceError


@asynccontextmanager
async def operation_lease(database, key, *, existing=None):
    key = lock_key(f"keystone.files:{key}")
    if existing is not None:
        await existing.acquire(key)
        yield existing
        return

    async with session_lease(
        database,
        keys=(key,),
        transaction_timeout=10,
        busy_error=ServiceError(409, "file_busy", "文件正在处理，请稍后重试"),
        interrupted_error=ServiceError(503, "file_coordination_lost", "文件协调连接已中断"),
    ) as lease:
        yield lease


@asynccontextmanager
async def file_lease(database, storage, identifier, *, existing=None):
    async with operation_lease(database, identifier, existing=existing) as lease:
        # Keep physical exclusion until local I/O ends, even if PostgreSQL disconnects.
        physical = storage.lease(identifier) if not storage.shared_files else nullcontext()
        with physical:
            yield lease

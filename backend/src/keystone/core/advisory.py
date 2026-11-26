"""Bounded session locks; guarded writes use the same physical connection."""

import asyncio
import hashlib
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from keystone.core.errors import ServiceError


def lock_key(name):
    return int.from_bytes(hashlib.sha256(name.encode()).digest()[:8], signed=True)


_TRY_LOCK = text("SELECT pg_try_advisory_lock(:key)")
_BACKEND_PID = text("SELECT pg_backend_pid()")


class SessionLease:
    def __init__(
        self, database, connection, pid, transaction_timeout, busy_error, interrupted_error
    ):
        self.database, self.connection, self.pid = database, connection, pid
        self.transaction_timeout = transaction_timeout
        self.transaction_lock = asyncio.Lock()
        self.busy_error = busy_error
        self.interrupted_error = interrupted_error
        self.lost = False

    async def check(self, session):
        if (
            self.lost
            or self.connection.invalidated
            or self.connection.closed
            or await session.scalar(_BACKEND_PID) != self.pid
        ):
            self.lost = True
            raise self.interrupted_error

    async def acquire(self, key):
        """Hold another lock on this connection until the enclosing lease exits."""
        async with self.transaction() as session:
            if not await session.scalar(_TRY_LOCK, {"key": key}):
                raise self.busy_error

    async def __call__(self):
        async with self.transaction():
            pass

    @asynccontextmanager
    async def transaction(self):
        async with asyncio.timeout(self.transaction_timeout), self.transaction_lock:
            try:
                async with (
                    AsyncSession(
                        bind=self.connection,
                        expire_on_commit=False,
                        info={"catalog": self.database.catalog},
                    ) as session,
                    session.begin(),
                ):
                    await self.check(session)
                    yield session
            except DBAPIError as error:
                if not (error.connection_invalidated or self.connection.invalidated):
                    raise
                self.lost = True
                raise self.interrupted_error from None


@asynccontextmanager
async def session_lease(
    database,
    *,
    keys=(),
    capacities=(),
    transaction_timeout=None,
    busy_error=None,
    interrupted_error=None,
):
    busy_error = busy_error or ServiceError(409, "model_refresh_busy", "该范围正在处理，请稍后重试")
    interrupted_error = interrupted_error or ServiceError(
        503, "model_refresh_interrupted", "协调连接已中断，旧结果不能写回"
    )
    async with database.engine.connect() as connection:
        try:
            for key in keys:
                if not await connection.scalar(_TRY_LOCK, {"key": key}):
                    raise busy_error
            for name, capacity in capacities:
                for index in range(capacity):
                    if await connection.scalar(
                        _TRY_LOCK,
                        {"key": lock_key(f"{name}:{index}")},
                    ):
                        break
                else:
                    raise ServiceError(409, "model_capacity_busy", "共享请求容量已满，请稍后重试")
            pid = await connection.scalar(_BACKEND_PID)
            await connection.commit()
            yield SessionLease(
                database, connection, pid, transaction_timeout, busy_error, interrupted_error
            )
        finally:
            # Never return acquired session locks (or an uncertain acquisition) to the pool.
            await connection.invalidate()

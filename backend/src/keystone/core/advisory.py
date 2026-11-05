"""Bounded session locks; guarded writes use the same physical connection."""

import asyncio
import hashlib
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from keystone.core.errors import ServiceError


def lock_key(name):
    return int.from_bytes(hashlib.sha256(name.encode()).digest()[:8], signed=True)


class SessionLease:
    def __init__(self, database, connection, pid, transaction_timeout=None):
        self.database, self.connection, self.pid = database, connection, pid
        self.transaction_timeout = transaction_timeout
        self.transaction_lock = asyncio.Lock()

    async def check(self, session):
        if (
            self.connection.invalidated
            or await session.scalar(text("SELECT pg_backend_pid()")) != self.pid
        ):
            raise ServiceError(503, "model_refresh_interrupted", "协调连接已中断，旧结果不能写回")

    async def __call__(self):
        async with self.transaction():
            pass

    @asynccontextmanager
    async def transaction(self):
        async with asyncio.timeout(self.transaction_timeout), self.transaction_lock:
            if self.connection.invalidated:
                raise ServiceError(
                    503, "model_refresh_interrupted", "协调连接已中断，旧结果不能写回"
                )
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


@asynccontextmanager
async def session_lease(database, *, keys=(), capacities=(), transaction_timeout=None):
    async with database.engine.connect() as connection:
        try:
            for key in keys:
                if not await connection.scalar(
                    text("SELECT pg_try_advisory_lock(:key)"), {"key": key}
                ):
                    raise ServiceError(409, "model_refresh_busy", "该范围正在处理，请稍后重试")
            for name, capacity in capacities:
                for index in range(capacity):
                    if await connection.scalar(
                        text("SELECT pg_try_advisory_lock(:key)"),
                        {"key": lock_key(f"{name}:{index}")},
                    ):
                        break
                else:
                    raise ServiceError(409, "model_capacity_busy", "共享请求容量已满，请稍后重试")
            pid = await connection.scalar(text("SELECT pg_backend_pid()"))
            await connection.commit()
            yield SessionLease(database, connection, pid, transaction_timeout)
        finally:
            # Never return acquired session locks (or an uncertain acquisition) to the pool.
            await connection.invalidate()

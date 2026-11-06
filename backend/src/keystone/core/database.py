"""Database lifecycle; no implicit DDL or SQLite fallback."""

import asyncio
from contextlib import asynccontextmanager

from alembic.migration import MigrationContext
from psycopg.errors import ConnectionTimeout
from sqlalchemy import MetaData, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeout
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from keystone.core.catalog import Catalog
from keystone.core.config import Settings
from keystone.core.errors import ServiceError


class Base(DeclarativeBase):
    metadata = MetaData(schema="keystone")


class Database:
    def __init__(self, settings: Settings, *, catalog: Catalog):
        if not isinstance(catalog, Catalog):
            raise TypeError("Database requires an explicit Catalog")
        self.catalog = catalog
        self.engine = (
            create_async_engine(
                settings.database_url.get_secret_value(),
                pool_pre_ping=True,
                pool_size=5,
                max_overflow=5,
                connect_args={"connect_timeout": 3},
                hide_parameters=True,
            )
            if settings.database_url
            else None
        )
        self.sessions = (
            async_sessionmaker(self.engine, expire_on_commit=False, info={"catalog": catalog})
            if self.engine
            else None
        )

    @asynccontextmanager
    async def transaction(self):
        if self.sessions is None:
            raise RuntimeError("Database is not configured")
        async with self.sessions() as session, session.begin():
            try:
                await session.connection()
            except (OperationalError, PoolTimeout) as error:
                if isinstance(error, PoolTimeout) or isinstance(error.orig, ConnectionTimeout):
                    raise ServiceError(
                        503, "database_connection_busy", "数据库连接暂不可用，请稍后重试"
                    ) from None
                raise
            yield session

    async def readiness(self, heads: set[str]) -> dict[str, str]:
        if self.engine is None:
            return {"database": "unconfigured", "schema": "unavailable"}
        connected = False
        try:
            async with asyncio.timeout(3), self.engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
                connected = True
                current = await connection.run_sync(
                    lambda sync: set(
                        MigrationContext.configure(
                            sync, opts={"version_table_schema": "keystone"}
                        ).get_current_heads()
                    )
                )
                return {"database": "ok", "schema": "ok" if current == heads else "out_of_date"}
        except Exception:
            # Driver errors can include credentials, hostnames or SQL parameters.
            return {"database": "ok" if connected else "unavailable", "schema": "unavailable"}

    async def close(self) -> None:
        if self.engine:
            await self.engine.dispose()

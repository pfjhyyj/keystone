"""Use an isolated database created by the test fixture, never an application DB."""

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text

from keystone.bootstrap import build_runtime
from keystone.core.config import Settings
from keystone.core.database import Base
from keystone.core.migrations import migration_config
from keystone.model_registry import MIGRATION_CHAINS
from keystone.platform.files.adapters.storage import initialize


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_empty_database_migration_and_transaction_rollback(empty_database_url, tmp_path):
    initialize(tmp_path / "storage")
    settings = Settings(
        config_file=None,
        database_url=empty_database_url,
        file_coordination_root=tmp_path / "storage",
        file_storages={"local": {"driver": "filesystem", "root": tmp_path / "storage"}},
    )
    runtime = build_runtime(settings)
    try:
        before = await runtime.readiness()
        assert before["checks"] == {"database": "ok", "schema": "out_of_date", "storage": "ok"}
        # Reading readiness must not create any schema or table.
        async with runtime.database.transaction() as session:
            assert not await session.scalar(
                text("SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname='keystone')")
            )
        config = migration_config(settings, MIGRATION_CHAINS)
        config.attributes["create_schema"] = True
        command.upgrade(config, "heads")
        command.upgrade(config, "heads")  # Repeated migration is idempotent.
        await runtime.bind_deployment()
        assert (await runtime.readiness())["status"] == "ready"
        async with runtime.database.engine.connect() as connection:
            diff = await connection.run_sync(
                lambda sync: compare_metadata(
                    MigrationContext.configure(
                        sync,
                        opts={
                            "include_schemas": True,
                            "version_table_schema": "keystone",
                            "compare_server_default": True,
                            "include_name": lambda name, type_, parents: (
                                type_ != "schema" or name == "keystone"
                            ),
                        },
                    ),
                    Base.metadata,
                )
            )
            assert diff == []
        assert (await runtime.database.readiness({"future_revision"}))["schema"] == "out_of_date"
        with pytest.raises(RuntimeError, match="rollback-test"):
            async with runtime.database.transaction() as session:
                await session.execute(text("CREATE TABLE keystone.rollback_probe (id integer)"))
                raise RuntimeError("rollback-test")
        async with runtime.database.transaction() as session:
            assert (
                await session.scalar(text("SELECT to_regclass('keystone.rollback_probe')")) is None
            )
    finally:
        await runtime.database.close()


@pytest.mark.postgres
def test_full_downgrade_removes_business_schema_and_preserves_dbos(empty_database_url):
    config = migration_config(Settings(config_file=None, database_url=empty_database_url))
    config.attributes["create_schema"] = True
    engine = create_engine(empty_database_url)
    try:
        # Even a full business rollback must leave the separate execution schema untouched.
        with engine.begin() as connection:
            connection.execute(text("CREATE SCHEMA dbos"))
            connection.execute(text("CREATE TABLE dbos.retention_probe (value integer)"))
            connection.execute(text("INSERT INTO dbos.retention_probe VALUES (42)"))
        command.upgrade(config, "head")
        tables = set(inspect(engine).get_table_names(schema="keystone"))
        command.downgrade(config, "base")
        assert set(inspect(engine).get_table_names(schema="keystone")) == {"alembic_version"}
        command.upgrade(config, "head")
        assert set(inspect(engine).get_table_names(schema="keystone")) == tables
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT value FROM dbos.retention_probe")) == 42
    finally:
        engine.dispose()

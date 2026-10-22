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
        config = migration_config(settings)
        config.attributes["create_schema"] = True
        command.upgrade(config, "head")
        command.upgrade(config, "head")  # Repeated deployment is idempotent.
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
        async with runtime.database.transaction() as session:
            tables = (
                (
                    await session.execute(
                        text("SELECT tablename FROM pg_tables WHERE schemaname='keystone'")
                    )
                )
                .scalars()
                .all()
            )
            assert set(tables) == {
                "alembic_version",
                "users",
                "roles",
                "user_roles",
                "role_permissions",
                "login_sessions",
                "personal_access_tokens",
                "audit_logs",
                "files",
                "file_grants",
                "file_references",
                "dictionaries",
                "dictionary_items",
                "model_channels",
                "model_entries",
                "model_invocations",
                "flow_runs",
                "worker_heartbeats",
                "worker_registrations",
            }
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
def test_initial_schema_is_complete_reversible_and_preserves_dbos(empty_database_url):
    domains = [
        (
            "identity",
            {
                "users",
                "roles",
                "user_roles",
                "role_permissions",
                "login_sessions",
                "personal_access_tokens",
            },
        ),
        ("audit", {"audit_logs"}),
        ("dictionary", {"dictionaries", "dictionary_items"}),
        ("files", {"files", "file_grants", "file_references"}),
        ("model_access", {"model_channels", "model_entries", "model_invocations"}),
        ("flow", {"flow_runs"}),
        ("core", {"worker_heartbeats"}),
    ]
    tables = set().union(*(tables for _, tables in domains))
    config = migration_config(Settings(config_file=None, database_url=empty_database_url))
    config.attributes["create_schema"] = True
    engine = create_engine(empty_database_url)
    try:
        # Even a full business rollback must leave the separate execution schema untouched.
        with engine.begin() as connection:
            connection.execute(text("CREATE SCHEMA dbos"))
            connection.execute(text("CREATE TABLE dbos.retention_probe (value integer)"))
            connection.execute(text("INSERT INTO dbos.retention_probe VALUES (42)"))
        expected = {"alembic_version"}
        for revision, created in domains:
            command.upgrade(config, revision)
            expected |= created
            assert set(inspect(engine).get_table_names(schema="keystone")) == expected
            with engine.connect() as connection:
                assert (
                    connection.scalar(text("SELECT version_num FROM keystone.alembic_version"))
                    == revision
                )
        columns = {
            column["name"]: column
            for column in inspect(engine).get_columns("files", schema="keystone")
        }
        assert not columns["storage_key"]["nullable"]
        assert columns["storage_key"]["default"] is None
        channel_columns = {
            column["name"]
            for column in inspect(engine).get_columns("model_channels", schema="keystone")
        }
        assert {"api_key_encrypted", "billing_api_key_encrypted"} <= channel_columns
        assert not {"credential_ref", "billing_credential_ref"} & channel_columns
        with engine.begin() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM keystone.alembic_version"))
                == "core"
            )
            connection.execute(
                text(
                    "INSERT INTO keystone.users "
                    "(id, username, display_name, password_hash, is_active, "
                    "password_change_required) "
                    "VALUES ('00000000-0000-4000-8000-000000000001', 'baseline_user', "
                    "'Baseline', 'not-a-login-credential', true, false)"
                )
            )
        command.upgrade(config, "head")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT username FROM keystone.users")) == "baseline_user"
        for index in reversed(range(len(domains))):
            command.downgrade(config, domains[index - 1][0] if index else "base")
            expected -= domains[index][1]
            assert set(inspect(engine).get_table_names(schema="keystone")) == expected
        command.upgrade(config, "head")
        assert set(inspect(engine).get_table_names(schema="keystone")) == tables | {
            "alembic_version",
            "worker_registrations",
        }
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT value FROM dbos.retention_probe")) == 42
    finally:
        engine.dispose()


@pytest.mark.postgres
def test_cost_migration_preserves_known_and_unknown_without_borrowing_current_key(
    empty_database_url,
):
    config = migration_config(Settings(config_file=None, database_url=empty_database_url))
    config.attributes["create_schema"] = True
    engine = create_engine(empty_database_url)
    try:
        command.upgrade(config, "core")
        with engine.begin() as connection:
            connection.execute(
                text("""
                INSERT INTO keystone.model_channels
                  (id,name,provider,api_base,api_key_encrypted,is_active,balance_check_status)
                VALUES ('00000000-0000-4000-8000-000000000001', 'original', 'openrouter',
                        'https://openrouter.ai/api/v1', 'opaque-current-key', true, 'never')
            """)
            )
            connection.execute(
                text("""
                INSERT INTO keystone.model_entries (id,key,channel_id,provider_model,is_active)
                VALUES ('00000000-0000-4000-8000-000000000002', 'chat',
                        '00000000-0000-4000-8000-000000000001', 'original-model', true)
            """)
            )
            for index, cost in enumerate(["0.002656", None], 3):
                connection.execute(
                    text("""
                    INSERT INTO keystone.model_invocations
                      (id,model_entry_id,channel_id,provider_model,upstream_request_id,status,
                       finished_at,cost_amount,cost_unit,cost_source,error_code)
                    VALUES (:id,'00000000-0000-4000-8000-000000000002',
                            '00000000-0000-4000-8000-000000000001','original-model',
                            'original-request','unknown', now(), :cost, :unit, :source,
                            'model_interrupted')
                """),
                    {
                        "id": f"00000000-0000-4000-8000-{index:012d}",
                        "cost": cost,
                        "unit": "credits" if cost else None,
                        "source": "query" if cost else "unknown",
                    },
                )
        command.upgrade(config, "head")
        with engine.connect() as connection:
            rows = connection.execute(
                text("""
                SELECT cost_amount,cost_source,status,error_code,cost_check_status,
                       cost_check_reason,cost_context,cost_next_check_at
                FROM keystone.model_invocations ORDER BY id
            """)
            ).all()
            assert str(rows[0].cost_amount) == "0.002656000000000000"
            assert rows[0].cost_source == "query" and rows[0].cost_check_status == "confirmed"
            assert rows[1].cost_amount is None and rows[1].cost_check_status == "unavailable"
            assert rows[1].cost_check_reason == "legacy_scope_unknown"
            for row in rows:
                assert row.status == "unknown" and row.error_code == "model_interrupted"
                assert row.cost_context is None and row.cost_next_check_at is None
        command.downgrade(config, "core")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM keystone.model_invocations")) == 2
    finally:
        engine.dispose()

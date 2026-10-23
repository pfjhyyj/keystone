"""Native Alembic recognizes old revision IDs and discovers new per-module scripts."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError

from keystone.core.config import Settings
from keystone.core.migrations import migration_config

pytestmark = pytest.mark.postgres
BACKEND = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "revision",
    [
        "identity",
        "audit",
        "dictionary",
        "files",
        "model_access",
        "flow",
        "core",
        "model_costs",
        "multi_workers",
        "worker_registration",
    ],
)
def test_database_from_flat_layout_upgrades_without_restamping(
    empty_database_url, tmp_path, revision
):
    settings = Settings(config_file=None, database_url=empty_database_url)
    current = migration_config(settings)
    current.attributes["create_schema"] = True
    scripts = ScriptDirectory.from_config(current)
    flat = tmp_path / "legacy-migrations"
    (flat / "versions").mkdir(parents=True)
    shutil.copyfile(Path(scripts.dir) / "env.py", flat / "env.py")
    for item in scripts.walk_revisions():
        shutil.copyfile(item.path, flat / "versions" / f"{item.revision}.py")
    legacy = migration_config(settings)
    legacy.set_main_option("script_location", str(flat))
    legacy.set_main_option("recursive_version_locations", "false")
    legacy.attributes["create_schema"] = True
    command.upgrade(legacy, revision)
    engine = create_engine(empty_database_url)
    try:
        with engine.begin() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM keystone.alembic_version"))
                == revision
            )
            connection.execute(
                text("""
                INSERT INTO keystone.users
                  (id,username,display_name,password_hash,is_active,password_change_required)
                VALUES ('00000000-0000-4000-8000-000000000001',
                        'preserved','Original','not-a-login-credential',true,false)
            """)
            )
            connection.execute(text("CREATE SCHEMA dbos"))
            connection.execute(text("CREATE TABLE dbos.retention_probe (value integer)"))
            connection.execute(text("INSERT INTO dbos.retention_probe VALUES (42)"))
        command.upgrade(current, "head")
        command.upgrade(current, "head")
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM keystone.alembic_version"))
                == "worker_registration"
            )
            assert connection.scalar(text("SELECT username FROM keystone.users")) == "preserved"
            assert connection.scalar(text("SELECT value FROM dbos.retention_probe")) == 42
        # The renamed scripts must also downgrade to every old revision. The old
        # flat artifact can then read that version and apply its own upgrades.
        command.downgrade(current, revision)
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM keystone.alembic_version"))
                == revision
            )
            assert connection.scalar(text("SELECT username FROM keystone.users")) == "preserved"
            assert connection.scalar(text("SELECT value FROM dbos.retention_probe")) == 42
        command.upgrade(legacy, "head")
        command.upgrade(current, "head")
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM keystone.alembic_version"))
                == "worker_registration"
            )
            assert connection.scalar(text("SELECT username FROM keystone.users")) == "preserved"
            assert connection.scalar(text("SELECT value FROM dbos.retention_probe")) == 42
    finally:
        engine.dispose()


@pytest.mark.parametrize("namespace,entity", [("notes", "note"), ("n" * 48, "e" * 14)])
def test_generated_business_migration_is_discovered_upgraded_and_reversible(
    empty_database_url, tmp_path, namespace, entity
):
    directory = tmp_path / "migrations"
    shutil.copytree(BACKEND / "migrations", directory, ignore=shutil.ignore_patterns("__pycache__"))
    result = subprocess.run(
        [
            sys.executable,
            str(BACKEND.parent / "scripts/new_business.py"),
            namespace,
            "--entity",
            entity,
            "--output",
            str(tmp_path / "businesses"),
            "--migrations",
            str(directory / "versions"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    config = migration_config(Settings(config_file=None, database_url=empty_database_url))
    config.set_main_option("script_location", str(directory))
    config.attributes["create_schema"] = True
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == ["0011"]
    assert script.get_revision("0011").down_revision == "worker_registration"
    assert (directory / "versions" / f"0011_{namespace}_initial.py").is_file()
    table = f"{namespace}_{entity}"
    engine = create_engine(empty_database_url)
    try:
        command.upgrade(config, "head")
        assert table in inspect(engine).get_table_names(schema="keystone")
        with engine.begin() as connection:
            connection.execute(
                text(
                    f"INSERT INTO keystone.{table} (id,title,content) VALUES "
                    "('00000000-0000-4000-8000-000000000001','Original','Preserved')"
                )
            )
        command.upgrade(config, "head")
        with engine.connect() as connection:
            assert connection.scalar(text(f"SELECT content FROM keystone.{table}")) == "Preserved"
        command.downgrade(config, "worker_registration")
        assert table not in inspect(engine).get_table_names(schema="keystone")
        assert "users" in inspect(engine).get_table_names(schema="keystone")
        command.upgrade(config, "head")
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM keystone.alembic_version"))
                == "0011"
            )
    finally:
        engine.dispose()


def test_refused_downgrade_rolls_back_the_entire_chain_and_preserves_dbos(empty_database_url):
    config = migration_config(Settings(config_file=None, database_url=empty_database_url))
    config.attributes["create_schema"] = True
    engine = create_engine(empty_database_url)
    try:
        command.upgrade(config, "head")
        with engine.begin() as connection:
            connection.execute(
                text("""
                INSERT INTO keystone.model_channels
                  (id,name,provider,api_base,api_key_encrypted,cost_scope,is_active,balance_check_status)
                VALUES ('00000000-0000-4000-8000-000000000001', 'preserved', 'openlux',
                        'https://example.invalid', 'opaque-not-a-credential',
                        'original-scope',true,'never')
            """)
            )
            connection.execute(
                text("""
                INSERT INTO keystone.worker_registrations(worker_id,state,retired_at)
                VALUES ('preserved-worker','retired',now())
            """)
            )
            connection.execute(text("CREATE SCHEMA dbos"))
            connection.execute(text("CREATE TABLE dbos.retention_probe (value integer)"))
            connection.execute(text("INSERT INTO dbos.retention_probe VALUES (42)"))
        with pytest.raises(DBAPIError, match="Remove new-provider data"):
            command.downgrade(config, "core")
        # Two later migrations have already executed their downgrade before the
        # provider guard fails. All their DDL and the version write must roll back.
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM keystone.alembic_version"))
                == "worker_registration"
            )
            assert (
                connection.scalar(text("SELECT state FROM keystone.worker_registrations"))
                == "retired"
            )
            assert connection.execute(
                text("SELECT provider,cost_scope FROM keystone.model_channels")
            ).one() == ("openlux", "original-scope")
            assert connection.scalar(text("SELECT value FROM dbos.retention_probe")) == 42
        assert {"worker_id", "cost_check_status"} <= {
            item["name"]
            for item in inspect(engine).get_columns("model_invocations", schema="keystone")
        }
    finally:
        engine.dispose()

"""Native Alembic recognizes the linear baseline and discovers generated scripts."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from keystone.core.config import Settings
from keystone.core.migrations import migration_config

pytestmark = pytest.mark.postgres
BACKEND = Path(__file__).resolve().parents[2]
SCRIPTS = ScriptDirectory.from_config(migration_config(Settings(config_file=None)))
CURRENT_HEAD = SCRIPTS.get_current_head()
BASELINE = tuple(revision.revision for revision in reversed(list(SCRIPTS.walk_revisions())))


@pytest.mark.parametrize("revision", BASELINE)
def test_baseline_revisions_upgrade_and_downgrade_without_restamping(
    empty_database_url, tmp_path, revision
):
    settings = Settings(config_file=None, database_url=empty_database_url)
    current = migration_config(settings)
    current.attributes["create_schema"] = True
    scripts = ScriptDirectory.from_config(current)
    flat = tmp_path / "flat-migrations"
    (flat / "versions").mkdir(parents=True)
    shutil.copyfile(Path(scripts.dir) / "env.py", flat / "env.py")
    for item in scripts.walk_revisions():
        shutil.copyfile(item.path, flat / "versions" / f"{item.revision}.py")
    standalone = migration_config(settings)
    standalone.set_main_option("script_location", str(flat))
    standalone.set_main_option("recursive_version_locations", "false")
    standalone.attributes["create_schema"] = True
    command.upgrade(standalone, revision)
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
                == CURRENT_HEAD
            )
            assert connection.scalar(text("SELECT username FROM keystone.users")) == "preserved"
            assert connection.scalar(text("SELECT value FROM dbos.retention_probe")) == 42
        # Numeric revisions must also downgrade and upgrade again from a standalone
        # script directory, so the chain stays usable outside the repository layout.
        command.downgrade(current, revision)
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM keystone.alembic_version"))
                == revision
            )
            assert connection.scalar(text("SELECT username FROM keystone.users")) == "preserved"
            assert connection.scalar(text("SELECT value FROM dbos.retention_probe")) == 42
        command.upgrade(standalone, "head")
        command.upgrade(current, "head")
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM keystone.alembic_version"))
                == CURRENT_HEAD
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
    child = f"{int(CURRENT_HEAD) + 1:04d}"
    assert script.get_heads() == [child]
    assert script.get_revision(child).down_revision == CURRENT_HEAD
    assert (directory / "versions" / f"{child}_{namespace}_initial.py").is_file()
    table = f"{namespace}_{entity}"
    engine = create_engine(empty_database_url)
    try:
        command.upgrade(config, "head")
        assert table in inspect(engine).get_table_names(schema="keystone")
        columns = {
            column["name"]: column
            for column in inspect(engine).get_columns(table, schema="keystone")
        }
        assert columns["owner_id"]["nullable"] is False
        owner_index = next(
            index
            for index in inspect(engine).get_indexes(table, schema="keystone")
            if index["column_names"] == ["owner_id"]
        )
        assert len(owner_index["name"]) <= 63
        with engine.begin() as connection:
            connection.execute(
                text(
                    f"INSERT INTO keystone.{table} (id,owner_id,title,content) VALUES "
                    "('00000000-0000-4000-8000-000000000001',"
                    "'00000000-0000-4000-8000-000000000002','Original','Preserved')"
                )
            )
        command.upgrade(config, "head")
        with engine.connect() as connection:
            assert connection.scalar(text(f"SELECT content FROM keystone.{table}")) == "Preserved"
            assert (
                str(connection.scalar(text(f"SELECT owner_id FROM keystone.{table}")))
                == "00000000-0000-4000-8000-000000000002"
            )
        command.downgrade(config, CURRENT_HEAD)
        assert table not in inspect(engine).get_table_names(schema="keystone")
        assert "users" in inspect(engine).get_table_names(schema="keystone")
        command.upgrade(config, "head")
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM keystone.alembic_version")) == child
            )
    finally:
        engine.dispose()

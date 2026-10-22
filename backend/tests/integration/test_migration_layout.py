"""Native Alembic recognizes old revision IDs and discovers new per-module scripts."""

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


@pytest.mark.parametrize(
    "revision", ["core", "model_costs", "multi_workers", "worker_registration"]
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
    finally:
        engine.dispose()


def test_generated_business_migration_is_discovered_upgraded_and_reversible(
    empty_database_url, tmp_path
):
    directory = tmp_path / "migrations"
    shutil.copytree(BACKEND / "migrations", directory, ignore=shutil.ignore_patterns("__pycache__"))
    result = subprocess.run(
        [
            sys.executable,
            str(BACKEND.parent / "scripts/new_business.py"),
            "notes",
            "--entity",
            "note",
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
    assert (directory / "versions/businesses/notes/0011_notes_initial.py").is_file()
    engine = create_engine(empty_database_url)
    try:
        command.upgrade(config, "head")
        assert "notes_note" in inspect(engine).get_table_names(schema="keystone")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO keystone.notes_note (id,title,content) VALUES "
                    "('00000000-0000-4000-8000-000000000001','Original','Preserved')"
                )
            )
        command.upgrade(config, "head")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT content FROM keystone.notes_note")) == "Preserved"
        command.downgrade(config, "worker_registration")
        assert "notes_note" not in inspect(engine).get_table_names(schema="keystone")
        assert "users" in inspect(engine).get_table_names(schema="keystone")
        command.upgrade(config, "head")
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM keystone.alembic_version"))
                == "0011"
            )
    finally:
        engine.dispose()

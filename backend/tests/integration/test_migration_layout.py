"""Native Alembic recognizes the linear baseline outside the repository layout."""

import shutil
from pathlib import Path

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text

from keystone.core.config import Settings
from keystone.core.migrations import migration_config

pytestmark = pytest.mark.postgres
SCRIPTS = ScriptDirectory.from_config(migration_config(Settings(config_file=None)))
CURRENT_HEAD = SCRIPTS.get_current_head()
BASELINE = tuple(revision.revision for revision in reversed(list(SCRIPTS.walk_revisions())))


def test_baseline_revisions_upgrade_and_downgrade_without_restamping(empty_database_url, tmp_path):
    revision = BASELINE[0]
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

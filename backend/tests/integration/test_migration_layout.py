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
    standalone.set_main_option("version_locations", str(flat / "versions"))
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


def write_revision(path, revision, down, *, labels=None, depends=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"revision = {revision!r}\ndown_revision = {down!r}\n"
        f"branch_labels = {labels!r}\ndepends_on = {depends!r}\n\n\n"
        "def upgrade():\n    pass\n\n\ndef downgrade():\n    pass\n",
        encoding="utf8",
    )


def test_business_chain_versions_match_what_alembic_records(
    empty_database_url, tmp_path, monkeypatch
):
    from keystone.core import migrations

    source = Path(migrations.__file__).resolve().parents[3] / "migrations"
    root = tmp_path / "app/migrations"
    shutil.copytree(source, root, ignore=shutil.ignore_patterns("__pycache__"))
    monkeypatch.setattr(
        migrations, "__file__", str(tmp_path / "app/.venv/site/keystone/core/migrations.py")
    )
    monkeypatch.chdir(tmp_path / "app")
    settings = Settings(config_file=None, database_url=empty_database_url)
    notes = root / "businesses/notes"
    engine = create_engine(empty_database_url)

    def upgrade():
        config = migration_config(settings, ("notes",))
        config.attributes["create_schema"] = True
        command.upgrade(config, "heads")
        return config

    def recorded():
        with engine.connect() as connection:
            return set(connection.scalars(text("SELECT version_num FROM keystone.alembic_version")))

    def expected():
        return migrations.expected_heads(settings, ("notes",))

    try:
        # The business depends on the platform head, so its row stands for both.
        write_revision(
            notes / "0001_initial.py", "notes_0001", None, labels=("notes",), depends=CURRENT_HEAD
        )
        upgrade()
        assert recorded() == expected() == {"notes_0001"}
        # The platform moves on without the business: both heads are recorded.
        platform_next = f"{int(CURRENT_HEAD) + 1:04d}"
        write_revision(
            root / f"versions/{platform_next}_probe_next.py", platform_next, CURRENT_HEAD
        )
        upgrade()
        assert recorded() == expected() == {platform_next, "notes_0001"}
        # A later business revision needs the newer platform structure.
        write_revision(notes / "0002_next.py", "notes_0002", "notes_0001", depends=platform_next)
        config = upgrade()
        assert recorded() == expected() == {"notes_0002"}
        # Removing the business chain leaves the platform at its head.
        command.downgrade(config, "notes@base")
        assert recorded() == {platform_next}
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT to_regclass('keystone.users') IS NOT NULL"))
        upgrade()
        assert recorded() == expected()
    finally:
        engine.dispose()

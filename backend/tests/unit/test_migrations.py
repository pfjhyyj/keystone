import re
import shutil
from pathlib import Path

import pytest
from alembic.script import ScriptDirectory

from keystone.core import migrations
from keystone.core.config import Settings


def test_source_migrations_are_independent_of_working_directory(tmp_path, monkeypatch):
    source = Path(migrations.__file__).resolve().parents[3] / "migrations"
    decoy = tmp_path / "migrations"
    decoy.mkdir()
    (decoy / "env.py").write_text("", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    settings = Settings(config_file=None)
    config = migrations.migration_config(settings)
    assert Path(config.get_main_option("script_location")) == source
    assert migrations.expected_heads(settings) == {"0010"}


def test_installed_package_uses_migrations_beside_working_directory(tmp_path, monkeypatch):
    source = Path(migrations.__file__).resolve().parents[3] / "migrations"
    app = tmp_path / "app"
    app.mkdir()
    shutil.copytree(source, app / "migrations", ignore=shutil.ignore_patterns("__pycache__"))
    installed = app / ".venv/lib/python3.12/site-packages/keystone/core/migrations.py"
    monkeypatch.setattr(migrations, "__file__", str(installed))
    monkeypatch.chdir(app)

    settings = Settings(config_file=None)
    config = migrations.migration_config(settings)
    assert Path(config.get_main_option("script_location")) == app / "migrations"
    assert migrations.expected_heads(settings) == {"0010"}


def test_missing_migration_files_fail_before_database_access(tmp_path, monkeypatch):
    monkeypatch.setattr(
        migrations, "__file__", str(tmp_path / "package/keystone/core/migrations.py")
    )
    monkeypatch.chdir(tmp_path)

    with pytest.raises(RuntimeError, match="Migration files missing"):
        migrations.migration_config(Settings(config_file=None))


def test_module_paths_and_global_sequence_follow_the_native_revision_chain():
    config = migrations.migration_config(Settings(config_file=None))
    script = ScriptDirectory.from_config(config)
    root = Path(script.dir) / "versions"
    chain = list(reversed(list(script.walk_revisions())))
    modules = (
        "identity",
        "audit",
        "dictionary",
        "files",
        "model_access",
        "flow",
        "worker",
    )
    modules += ("model_access",)
    modules += ("model_access",)
    modules += ("model_access",)
    assert len(chain) == len(modules)
    assert len(script.get_heads()) == 1 and len(script.get_bases()) == 1
    assert {Path(revision.path) for revision in chain} == set(root.rglob("*.py"))
    previous = None
    for number, revision in enumerate(chain, 1):
        path = Path(revision.path)
        parts = path.relative_to(root).parts
        assert len(parts) == 1, path
        assert re.fullmatch(
            rf"{number:04d}_{modules[number - 1]}_[a-z][a-z0-9_]*\.py", path.name
        ), path
        assert revision.down_revision == previous
        assert revision.revision == f"{number:04d}"
        assert not revision.branch_labels and not revision.dependencies
        previous = revision.revision

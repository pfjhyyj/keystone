import shutil
from pathlib import Path

import pytest

from keystone.core import migrations
from keystone.core.config import Settings


def test_source_migrations_are_independent_of_working_directory(tmp_path, monkeypatch):
    source = Path(migrations.__file__).resolve().parents[3] / "migrations"
    decoy = tmp_path / "migrations"
    decoy.mkdir()
    (decoy / "env.py").write_text("", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    settings = Settings(_env_file=None)
    config = migrations.migration_config(settings)
    assert Path(config.get_main_option("script_location")) == source
    assert migrations.expected_heads(settings) == {"0006_flow"}


def test_installed_package_uses_migrations_beside_working_directory(tmp_path, monkeypatch):
    source = Path(migrations.__file__).resolve().parents[3] / "migrations"
    app = tmp_path / "app"
    app.mkdir()
    shutil.copytree(source, app / "migrations", ignore=shutil.ignore_patterns("__pycache__"))
    installed = app / ".venv/lib/python3.12/site-packages/keystone/core/migrations.py"
    monkeypatch.setattr(migrations, "__file__", str(installed))
    monkeypatch.chdir(app)

    settings = Settings(_env_file=None)
    config = migrations.migration_config(settings)
    assert Path(config.get_main_option("script_location")) == app / "migrations"
    assert migrations.expected_heads(settings) == {"0006_flow"}


def test_missing_migration_files_fail_before_database_access(tmp_path, monkeypatch):
    monkeypatch.setattr(
        migrations, "__file__", str(tmp_path / "package/keystone/core/migrations.py")
    )
    monkeypatch.chdir(tmp_path)

    with pytest.raises(RuntimeError, match="Migration files missing"):
        migrations.migration_config(Settings(_env_file=None))

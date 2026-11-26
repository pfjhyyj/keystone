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
    assert migrations.expected_heads(settings) == {"0011"}


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
    assert migrations.expected_heads(settings) == {"0011"}


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
        "environments",
        "messaging",
        "flow",
        "core",
    )
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


def business_revision(directory, number, *, down=None, labels=None, depends=None, name=None):
    namespace = directory.name
    revision = name or f"{namespace}_{number:04d}"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{number:04d}_change.py").write_text(
        f"revision = {revision!r}\n"
        f"down_revision = {down!r}\n"
        f"branch_labels = {labels!r}\n"
        f"depends_on = {depends!r}\n\n\n"
        "def upgrade():\n    pass\n\n\n"
        "def downgrade():\n    pass\n",
        encoding="utf8",
    )


@pytest.fixture
def layout(tmp_path, monkeypatch):
    source = Path(migrations.__file__).resolve().parents[3] / "migrations"
    app = tmp_path / "app"
    shutil.copytree(source, app / "migrations", ignore=shutil.ignore_patterns("__pycache__"))
    installed = app / ".venv/lib/python3.12/site-packages/keystone/core/migrations.py"
    monkeypatch.setattr(migrations, "__file__", str(installed))
    monkeypatch.chdir(app)
    return app / "migrations"


def test_business_chains_add_their_own_heads_only_when_registered(layout):
    notes = layout / "businesses/notes"
    business_revision(notes, 1, labels=("notes",), depends="0008")
    business_revision(notes, 2, down="notes_0001", depends="0011")
    business_revision(layout / "businesses/unlisted", 1, labels=("unlisted",), depends="0011")
    settings = Settings(config_file=None)

    assert migrations.expected_heads(settings) == {"0011"}
    # notes_0002 depends on the platform head, so Alembic records the platform through it.
    assert migrations.expected_heads(settings, ("notes",)) == {"notes_0002"}
    script = migrations.migration_scripts(migrations.migration_config(settings, ("notes",)))
    assert script.get_revision("notes_0001").dependencies == "0008"


@pytest.mark.parametrize("chains", [("missing",), ("Notes",), ("n" * 28,), ("notes", "notes")])
def test_unregistered_or_invalid_chain_names_fail_before_loading(layout, chains):
    (layout / "businesses/notes").mkdir(parents=True)
    with pytest.raises(RuntimeError, match="chain"):
        migrations.migration_config(Settings(config_file=None), chains)


@pytest.mark.parametrize(
    "mistake,message",
    [
        ("unlabelled_base", "take their label once"),
        ("relabelled_child", "take their label once"),
        ("business_dependency", "depend only on the platform"),
        ("platform_dependency", "Platform migrations cannot depend"),
        ("gap", "contiguous"),
        ("renamed_revision", "name and revision disagree"),
    ],
)
def test_malformed_chains_are_rejected(layout, mistake, message):
    notes, other = layout / "businesses/notes", layout / "businesses/other"
    business_revision(other, 1, labels=("other",), depends="0009")
    first = {"labels": ("notes",), "depends": "0009"}
    if mistake == "unlabelled_base":
        first = {"depends": "0009"}
    business_revision(notes, 1, **first)
    if mistake == "relabelled_child":
        business_revision(notes, 2, down="notes_0001", labels=("again",))
    elif mistake == "business_dependency":
        business_revision(notes, 2, down="notes_0001", depends="other_0001")
    elif mistake == "gap":
        business_revision(notes, 3, down="notes_0001")
    elif mistake == "renamed_revision":
        business_revision(notes, 2, down="notes_0001", name="notes_0007")
    elif mistake == "platform_dependency":
        path = layout / "versions/0009_messaging_initial.py"
        path.write_text(
            path.read_text(encoding="utf8").replace(
                "depends_on = None", 'depends_on = "notes_0001"'
            ),
            encoding="utf8",
        )
    config = migrations.migration_config(Settings(config_file=None), ("notes", "other"))
    with pytest.raises(RuntimeError, match=message):
        migrations.migration_scripts(config)

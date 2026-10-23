"""Business scaffold checks without a database."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from alembic.script import ScriptDirectory
from support.module_checks import check_module_layers

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT.parent / "scripts/new_business.py"
VERSIONS = ROOT / "migrations/versions"


def migration_head(directory):
    script = ScriptDirectory(str(directory.parent), version_locations=[str(directory)])
    heads = set(script.get_heads())
    assert len(heads) == 1, heads
    return heads.pop()


def invoke(businesses, migrations, *extra):
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "tiny_notes",
            "--entity",
            "tiny_note",
            "--label",
            "小笔记",
            "--output",
            str(businesses),
            "--migrations",
            str(migrations),
            *extra,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def generate(tmp_path, *extra):
    businesses = tmp_path / "businesses"
    migrations = tmp_path / "migrations"
    shutil.copytree(VERSIONS, migrations)
    return invoke(businesses, migrations, *extra), businesses, migrations


def test_generated_business_is_importable_and_migration_extends_head(tmp_path, monkeypatch):
    head = migration_head(VERSIONS)
    result, businesses, migrations = generate(tmp_path)
    assert result.returncode == 0, result.stderr
    package = businesses / "tiny_notes"
    check_module_layers(package, "keystone.businesses.tiny_notes")
    assert {path.name for path in package.iterdir()} == {
        "__init__.py",
        "catalog.py",
        "models",
        "schemas",
        "services",
        "controllers",
    }
    path = migrations / "0008_tiny_notes_initial.py"
    migration = path.read_text(encoding="utf-8")
    assert 'revision = "0008"' in migration
    assert f'down_revision = "{head}"' in migration
    assert '"tiny_notes_tiny_note"' in migration
    assert migration_head(migrations) == "0008"

    monkeypatch.syspath_prepend(str(businesses))
    from tiny_notes.catalog import CATALOG
    from tiny_notes.controllers import router
    from tiny_notes.models import TinyNote
    from tiny_notes.schemas import TinyNoteCreate
    from tiny_notes.services import TinyNoteService

    assert CATALOG.permissions == {
        "tiny_notes.tiny_note:read": "查看小笔记",
        "tiny_notes.tiny_note:write": "保存小笔记",
    }
    assert CATALOG.audit_actions["tiny_notes.tiny_note.save"] == {"before", "after"}
    assert TinyNote.__tablename__ == "tiny_notes_tiny_note"
    assert TinyNoteCreate(title="标题").content == ""
    assert TinyNoteService and router
    # Importing a scaffolded model must not leak into later migration metadata checks.
    from keystone.core.database import Base

    Base.metadata.remove(TinyNote.__table__)
    assert "tiny_notes_tiny_note" not in Base.metadata.tables

    formatted = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "format",
            "--check",
            "--config",
            str(ROOT / "pyproject.toml"),
            str(package),
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert formatted.returncode == 0, formatted.stderr


def test_scaffold_refuses_existing_package_and_invalid_namespace(tmp_path):
    result, businesses, _ = generate(tmp_path)
    assert result.returncode == 0, result.stderr
    migrations = tmp_path / "migrations"
    repeated = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "tiny_notes",
            "--entity",
            "tiny_note",
            "--output",
            str(businesses),
            "--migrations",
            str(migrations),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert repeated.returncode != 0 and "already exists" in repeated.stderr

    invalid = subprocess.run(
        [sys.executable, str(SCRIPT), "Invalid"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert invalid.returncode != 0 and "must match" in invalid.stderr


@pytest.mark.parametrize("extra", [("--no-migration",)])
def test_scaffold_can_skip_the_migration(tmp_path, extra):
    result, _, migrations = generate(tmp_path, *extra)
    assert result.returncode == 0, result.stderr
    assert not list(migrations.glob("*_tiny_notes_initial.py"))


def test_scaffold_continues_global_sequence_across_business_modules(tmp_path):
    result, businesses, migrations = generate(tmp_path)
    assert result.returncode == 0, result.stderr
    # A long namespace must not overflow Alembic's 32-character revision column.
    namespace = "a" * 48
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            namespace,
            "--entity",
            "note",
            "--output",
            str(businesses),
            "--migrations",
            str(migrations),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    path = migrations / f"0009_{namespace}_initial.py"
    value = path.read_text(encoding="utf8")
    assert 'revision = "0009"' in value and 'down_revision = "0008"' in value
    assert migration_head(migrations) == "0009"


def test_scaffold_reads_annotated_metadata_without_executing_migration(tmp_path):
    businesses, migrations = tmp_path / "businesses", tmp_path / "migrations"
    shutil.copytree(VERSIONS, migrations)
    path = migrations / "0007_worker_initial.py"
    content = (
        path.read_text(encoding="utf8")
        .replace('revision = "0007"', 'revision: str = "0007"')
        .replace('down_revision = "0006"', 'down_revision: str | None = "0006"')
        .replace("branch_labels = None", "branch_labels: tuple[str, ...] | None = None")
        .replace("depends_on = None", "depends_on: str | None = None")
    )
    path.write_text(content, encoding="utf8")
    assert migration_head(migrations) == "0007"
    path.write_text(
        content + '\nraise RuntimeError("must not import historical DDL")\n', encoding="utf8"
    )
    result = invoke(businesses, migrations)
    assert result.returncode == 0, result.stderr
    value = (migrations / "0008_tiny_notes_initial.py").read_text(encoding="utf8")
    assert 'revision = "0008"' in value and 'down_revision = "0007"' in value


def test_scaffold_refuses_to_recreate_a_module_with_existing_migrations(tmp_path):
    result, _, migrations = generate(tmp_path)
    assert result.returncode == 0, result.stderr
    before = {path: path.read_bytes() for path in migrations.rglob("*.py")}
    output = tmp_path / "another-output"
    result = invoke(output, migrations)
    assert (
        result.returncode != 0
        and "Initial migration for tiny_notes already exists" in result.stderr
    )
    assert not output.exists()
    assert {path: path.read_bytes() for path in migrations.rglob("*.py")} == before
    result = invoke(output, migrations, "--no-migration")
    assert result.returncode == 0, result.stderr
    assert {path: path.read_bytes() for path in migrations.rglob("*.py")} == before


@pytest.mark.parametrize("entity_length,valid", [(14, True), (15, False)])
def test_scaffold_checks_postgresql_table_name_length_before_writing(
    tmp_path, entity_length, valid
):
    output, migrations = tmp_path / "businesses", tmp_path / "migrations"
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "n" * 48,
            "--entity",
            "e" * entity_length,
            "--output",
            str(output),
            "--migrations",
            str(migrations),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if valid:
        assert result.returncode == 0, result.stderr
        assert migration_head(migrations) == "0001"
    else:
        assert result.returncode != 0 and "table name must be at most 63" in result.stderr
        assert not output.exists() and not migrations.exists()


@pytest.mark.parametrize(
    "mistake,message",
    [
        ("duplicate_sequence", "Duplicate migration"),
        ("duplicate_revision", "Duplicate migration"),
        ("missing_sequence", "contiguous"),
        ("wrong_parent", "linear chain"),
        ("missing_module", "Invalid migration path/name"),
        ("module_directory", "Invalid migration path/name"),
        ("merge", "Invalid linear migration metadata"),
        ("nested_category", "Invalid migration path/name"),
        ("annotated_branch", "Invalid linear migration metadata"),
        ("annotated_dependency", "Invalid linear migration metadata"),
        ("duplicate_metadata", "Invalid static migration metadata"),
        ("dynamic_metadata", "Invalid static migration metadata"),
        ("syntax_error", "Invalid static migration metadata"),
    ],
)
def test_invalid_migration_history_fails_before_creating_business(tmp_path, mistake, message):
    businesses, migrations = tmp_path / "businesses", tmp_path / "migrations"
    shutil.copytree(VERSIONS, migrations)
    path = migrations / "0007_worker_initial.py"
    content = path.read_text(encoding="utf8")
    if mistake == "duplicate_sequence":
        path.rename(path.with_name("0006_worker_initial.py"))
    elif mistake == "duplicate_revision":
        path.write_text(
            content.replace('revision = "0007"', 'revision = "0006"'),
            encoding="utf8",
        )
    elif mistake == "missing_sequence":
        path.rename(path.with_name("0008_worker_initial.py"))
    elif mistake == "wrong_parent":
        path.write_text(
            content.replace('down_revision = "0006"', 'down_revision = "0004"'),
            encoding="utf8",
        )
    elif mistake == "missing_module":
        path.rename(path.with_name("0007_registration.py"))
    elif mistake == "module_directory":
        target = migrations / "worker" / path.name
        target.parent.mkdir()
        path.rename(target)
    elif mistake == "merge":
        path.write_text(
            content.replace('down_revision = "0006"', 'down_revision = ("0006", "0005")'),
            encoding="utf8",
        )
    elif mistake == "nested_category":
        target = migrations / "platform/worker" / path.name
        target.parent.mkdir(parents=True)
        path.rename(target)
    elif mistake == "annotated_branch":
        path.write_text(
            content.replace(
                "branch_labels = None", 'branch_labels: tuple[str, ...] | None = ("worker",)'
            ),
            encoding="utf8",
        )
    elif mistake == "annotated_dependency":
        path.write_text(
            content.replace("depends_on = None", 'depends_on: str | None = "0001"'),
            encoding="utf8",
        )
    elif mistake == "duplicate_metadata":
        path.write_text(content + '\ndown_revision = "0006"\n', encoding="utf8")
    elif mistake == "dynamic_metadata":
        path.write_text(
            content.replace('revision = "0007"', 'revision = str("0007")'),
            encoding="utf8",
        )
    elif mistake == "syntax_error":
        path.write_text(content + "\ninvalid = (\n", encoding="utf8")
    result = invoke(businesses, migrations)
    assert result.returncode != 0 and message in result.stderr, result.stderr
    assert not businesses.exists()

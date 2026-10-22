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
    script = ScriptDirectory(
        str(directory.parent), version_locations=[str(directory)], recursive_version_locations=True
    )
    heads = set(script.get_heads())
    assert len(heads) == 1, heads
    return heads.pop()


def generate(tmp_path, *extra):
    businesses = tmp_path / "businesses"
    migrations = tmp_path / "migrations"
    shutil.copytree(VERSIONS, migrations)
    result = subprocess.run(
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
    return result, businesses, migrations


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
    path = migrations / "businesses/tiny_notes/0011_tiny_notes_initial.py"
    migration = path.read_text(encoding="utf-8")
    assert 'revision = "0011"' in migration
    assert f'down_revision = "{head}"' in migration
    assert '"tiny_notes_tiny_note"' in migration
    assert migration_head(migrations) == "0011"

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
    assert not (migrations / "businesses").exists()


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
    path = migrations / "businesses" / namespace / f"0012_{namespace}_initial.py"
    value = path.read_text(encoding="utf8")
    assert 'revision = "0012"' in value and 'down_revision = "0011"' in value
    assert migration_head(migrations) == "0012"


@pytest.mark.parametrize(
    "mistake,message",
    [
        ("duplicate_sequence", "Duplicate migration"),
        ("duplicate_revision", "Duplicate migration"),
        ("missing_sequence", "contiguous"),
        ("wrong_parent", "linear chain"),
        ("wrong_owner", "Invalid migration path/name"),
        ("flat_file", "Invalid migration path/name"),
        ("merge", "Invalid linear migration metadata"),
    ],
)
def test_invalid_migration_history_fails_before_creating_business(tmp_path, mistake, message):
    businesses, migrations = tmp_path / "businesses", tmp_path / "migrations"
    shutil.copytree(VERSIONS, migrations)
    path = migrations / "core/0010_core_worker_registration.py"
    content = path.read_text(encoding="utf8")
    if mistake == "duplicate_sequence":
        path.rename(path.with_name("0009_core_worker_registration.py"))
    elif mistake == "duplicate_revision":
        path.write_text(
            content.replace('revision = "worker_registration"', 'revision = "core"'),
            encoding="utf8",
        )
    elif mistake == "missing_sequence":
        path.rename(path.with_name("0012_core_worker_registration.py"))
    elif mistake == "wrong_parent":
        path.write_text(
            content.replace('down_revision = "multi_workers"', 'down_revision = "identity"'),
            encoding="utf8",
        )
    elif mistake == "wrong_owner":
        path.rename(path.with_name("0010_files_worker_registration.py"))
    elif mistake == "flat_file":
        path.rename(migrations / path.name)
    elif mistake == "merge":
        path.write_text(
            content.replace(
                'down_revision = "multi_workers"', 'down_revision = ("multi_workers", "flow")'
            ),
            encoding="utf8",
        )
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "tiny_notes",
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
    assert result.returncode != 0 and message in result.stderr, result.stderr
    assert not businesses.exists()

"""Business scaffold checks without a database."""

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from support.module_checks import check_module_layers

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT.parent / "scripts/new_business.py"
VERSIONS = ROOT / "migrations/versions"
REVISION = re.compile(r'^revision = "([^"]+)"', re.MULTILINE)
DOWN_REVISION = re.compile(r'^down_revision = "([^"]+)"', re.MULTILINE)


def migration_head(directory):
    revisions = {}
    for path in sorted(directory.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        revision, down = REVISION.search(text), DOWN_REVISION.search(text)
        if revision and down:
            revisions[revision.group(1)] = down.group(1)
    heads = set(revisions) - set(revisions.values())
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
    migration = (migrations / "tiny_notes_init.py").read_text(encoding="utf-8")
    assert 'revision = "tiny_notes_init"' in migration
    assert f'down_revision = "{head}"' in migration
    assert '"tiny_notes_tiny_note"' in migration

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
            str(migrations / "tiny_notes_init.py"),
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
    assert not list(migrations.glob("tiny_notes_init.py"))

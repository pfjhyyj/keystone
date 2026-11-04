"""Business declarations and import boundaries require no database."""

import subprocess
import sys
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from support.business_api import create_app
from support.business_catalog import (
    NOTES_ACTION as ACTION,
)
from support.business_catalog import (
    NOTES_CATALOG as DECLARATION,
)
from support.business_catalog import (
    NOTES_READ as READ,
)
from support.business_catalog import (
    NOTES_WRITE as WRITE,
)
from support.flow_packages import package_for_definitions

from keystone import composition
from keystone.core.catalog import Catalog
from keystone.core.config import Settings


@pytest.mark.parametrize(
    "declarations",
    [
        (("test_notes", DECLARATION), ("test_notes", DECLARATION)),
        (("identity", Catalog()),),
        (("file", Catalog()),),
        (("invalid-key", Catalog()),),
        (("test_notes", Catalog(permissions={"user:read": "Override"})),),
        (("test_notes", Catalog(permissions={READ: " "})),),
        (("test_notes", Catalog(permissions={"test_notes.note..draft:read": "Read"})),),
        (("test_notes", Catalog(permissions={"test_notes.note.:read": "Read"})),),
        (("test_notes", Catalog(permissions={"test_notes.note:" + "x" * 100: "Long"})),),
        (("test_notes", Catalog(audit_actions={"identity.login": set()})),),
        (("test_notes", Catalog(audit_actions={ACTION: {"invalid-field"}})),),
    ],
)
def test_invalid_business_declarations_fail_at_startup(monkeypatch, declarations):
    monkeypatch.setattr(composition, "BUSINESS_CATALOGS", declarations)
    with pytest.raises(ValueError, match="business"):
        with TestClient(create_app(Settings(config_file=None))):
            pytest.fail("Invalid declarations must prevent startup")


def test_catalog_copies_input_and_is_immutable():
    permissions, fields = {READ: "Read"}, {"status"}
    catalog = Catalog(permissions, {ACTION: fields})
    permissions[WRITE] = "Write"
    fields.add("secret")
    assert WRITE not in catalog.permissions
    assert catalog.audit_actions[ACTION] == {"status"}
    with pytest.raises(TypeError):
        catalog.permissions[WRITE] = "Write"
    with pytest.raises(AttributeError):
        catalog.audit_actions[ACTION].add("secret")


@pytest.mark.parametrize("fields", ["status", b"status"])
def test_audit_fields_reject_string_input(fields):
    with pytest.raises(ValueError, match="collection"):
        Catalog(audit_actions={ACTION: fields})


def test_flow_permissions_must_be_in_application_catalog(monkeypatch):
    from support.flow_fixture import contracts

    from keystone.bootstrap import build_runtime

    monkeypatch.setattr(
        composition,
        "FLOW_PACKAGES",
        (
            package_for_definitions(
                (replace(contracts.definition, required_permissions=(READ, "model:invoke")),)
            ),
        ),
    )
    with pytest.raises(ValueError, match="undeclared permissions"):
        build_runtime(Settings(config_file=None))


def test_all_flow_definitions_are_validated_and_directory_is_frozen(monkeypatch):
    from support.business_catalog import install_catalog

    install_catalog(monkeypatch)
    from support.flow_fixture.contracts import definition

    from keystone.composition import build_catalog
    from keystone.platform.flow.schemas.definitions import build_definitions

    with pytest.raises(ValueError, match="duplicate"):
        build_definitions(
            (definition, replace(definition, name="Duplicate")), build_catalog().permissions
        )
    with pytest.raises(ValueError, match="undeclared"):
        build_definitions(
            (
                definition,
                replace(definition, key="second", required_permissions=(READ, "model:invoke")),
            ),
            build_catalog().permissions,
        )
    directory = build_definitions((definition,), build_catalog().permissions)
    with pytest.raises(TypeError):
        directory[definition.key] = replace(definition, name="Changed")


@pytest.mark.parametrize("extra_business_model", [False, True])
def test_model_registration_does_not_load_api_or_workflow(extra_business_model):
    # A clean interpreter detects accidental imports hidden by pytest collection.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            f"EXTRA_BUSINESS_MODEL = {extra_business_model!r}\n"
            + """
import sys
from keystone.model_registry import load_models
if EXTRA_BUSINESS_MODEL:
    from keystone.core.database import Base
    from sqlalchemy.orm import Mapped, mapped_column
    class Probe(Base):
        __tablename__ = 'test_notes_probe'
        id: Mapped[int] = mapped_column(primary_key=True)
metadata = load_models()
expected = {
    'users', 'roles', 'user_roles', 'role_permissions', 'login_sessions',
    'personal_access_tokens', 'audit_logs', 'files', 'file_references',
    'dictionaries', 'dictionary_items', 'model_channels', 'model_entries',
    'model_invocations', 'flow_runs',
}
assert {'keystone.' + name for name in expected} <= metadata.tables.keys()
assert load_models() is metadata
assert all(table.schema == 'keystone' for table in metadata.tables.values())
assert 'fastapi' not in sys.modules
assert 'dbos' not in sys.modules
assert not any('.workflow' in name or '.agents' in name for name in sys.modules)
assert not any('.controllers' in name for name in sys.modules)
""",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr

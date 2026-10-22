"""Test-only declarations exercise extension seams without adding a business domain."""

import subprocess
import sys
from dataclasses import replace
from functools import partial
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from test_files_dictionary import client as client
from test_identity import (
    NEW_PASSWORD,
    PASSWORD,
    assign,
    bearer,
    create_user,
    login,
    query,
    role,
    token,
)

from keystone import composition
from keystone.bootstrap import create_app
from keystone.core.asyncio import new_event_loop
from keystone.core.catalog import Catalog
from keystone.core.config import Settings
from keystone.core.errors import ServiceError
from keystone.platform.audit.models import AuditLog
from keystone.platform.audit.services import writer as audit
from keystone.platform.identity.schemas.credentials import Credential
from keystone.platform.identity.services.access import AccessContext
from keystone.platform.identity.services.authorization import load_authorization

READ = "test_notes.note:read"
WRITE = "test_notes.note:write"
ACTION = "test_notes.note.save"
DECLARATION = Catalog(
    permissions={READ: "Read test notes", WRITE: "Write test notes"},
    audit_actions={ACTION: {"after", "status"}},
)


def install(monkeypatch):
    monkeypatch.setattr(composition, "BUSINESS_CATALOGS", (("test_notes", DECLARATION),))


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
        "FLOW_DEFINITIONS",
        (replace(contracts.definition, required_permissions=(READ,)),),
    )
    with pytest.raises(ValueError, match="undeclared permissions"):
        build_runtime(Settings(config_file=None))


def test_all_flow_definitions_are_validated_and_directory_is_frozen():
    from support.flow_fixture.contracts import definition

    from keystone.composition import PLATFORM_CATALOG
    from keystone.platform.flow.schemas.definitions import build_definitions

    with pytest.raises(ValueError, match="duplicate"):
        build_definitions(
            (definition, replace(definition, name="Duplicate")), PLATFORM_CATALOG.permissions
        )
    with pytest.raises(ValueError, match="undeclared"):
        build_definitions(
            (definition, replace(definition, key="second", required_permissions=(READ,))),
            PLATFORM_CATALOG.permissions,
        )
    directory = build_definitions((definition,), PLATFORM_CATALOG.permissions)
    with pytest.raises(TypeError):
        directory[definition.key] = replace(definition, name="Changed")


@pytest.mark.postgres
def test_flow_directory_isolation_in_api_execution_and_reconciliation(monkeypatch, request):
    from support.flow_fixture import contracts

    from keystone.core.schemas import Input
    from keystone.platform.flow.services.identity import ExecutionIdentity
    from keystone.platform.flow.tasks.workflows import reconcile_one

    class EmptyInput(Input):
        pass

    first_definition = replace(
        contracts.definition,
        input_type=EmptyInput,
        file_fields=(),
        model_field=None,
        required_permissions=(READ,),
    )
    second_definition = replace(
        first_definition, name="Second application", required_permissions=(WRITE,)
    )
    install(monkeypatch)
    monkeypatch.setattr(composition, "FLOW_DEFINITIONS", (first_definition,))
    first = request.getfixturevalue("client")
    first_runtime = first.app.state.runtime
    first_runtime.settings.flow_enabled = True
    login(first)
    read_pat = bearer(token(first, ["flow:start", "flow:read", READ])["token"])
    write_pat = bearer(token(first, ["flow:start", "flow:read", WRITE])["token"])
    monkeypatch.setattr(composition, "FLOW_DEFINITIONS", (second_definition,))
    settings = first_runtime.settings.model_copy(update={"flow_enabled": False})
    with TestClient(
        create_app(settings), backend_options={"loop_factory": new_event_loop}
    ) as second:
        second_runtime = second.app.state.runtime
        second_runtime.settings.flow_enabled = True
        for app, allowed, denied, expected in (
            (first, read_pat, write_pat, first_definition),
            (second, write_pat, read_pat, second_definition),
        ):
            visible = app.get("/api/v1/flow-definitions", headers=allowed)
            assert visible.status_code == 200
            assert visible.json()["items"] == [
                expected.view()
                | {
                    "required_permissions": list(expected.required_permissions),
                    "file_fields": [],
                    "output_file_fields": list(expected.output_file_fields),
                }
            ]
            assert app.get("/api/v1/flow-definitions", headers=denied).json()["items"] == []
            payload = {"definition_key": expected.key, "request_key": expected.name, "input": {}}
            assert app.post("/api/v1/flow-runs", headers=denied, json=payload).status_code == 403
            response = app.post("/api/v1/flow-runs", headers=allowed, json=payload)
            assert response.status_code == 202, response.text
            run_id = response.json()["id"]

            async def execute(app=app, run_id=run_id, expected=expected):
                runtime = app.app.state.runtime
                async with ExecutionIdentity(runtime, run_id).access():
                    pass

                class Engine:
                    async def state(self, identifier):
                        return None

                    async def enqueue(self, run, definition):
                        assert definition is expected
                        assert str(run.id) == run_id
                        self.enqueued = True

                engine = Engine()
                await reconcile_one(runtime, engine, UUID(run_id))
                assert engine.enqueued

            app.portal.call(execute)


@pytest.mark.postgres
@pytest.mark.parametrize("expansion", ["default", "serializer", "model_selection"])
@pytest.mark.parametrize("extra_bytes", [0, 1])
def test_flow_final_input_size_limit(monkeypatch, request, expansion, extra_bytes):
    from pydantic import Field, field_serializer
    from support.flow_fixture import contracts
    from test_model_access import channel, entry

    from keystone.core.schemas import Input
    from keystone.platform.files.models import FileReference
    from keystone.platform.flow.models import FlowRun
    from keystone.platform.flow.schemas.values import small_json
    from keystone.platform.model_access.services.entries import select_model

    padding = ""

    class Params(Input):
        text: str = Field(default_factory=lambda: padding)
        model_key: str = "chat"

        @field_serializer("text")
        def serialize_text(self, value) -> str:
            return padding if expansion == "serializer" and value == "expand" else value

    monkeypatch.setattr(
        composition,
        "FLOW_DEFINITIONS",
        (
            replace(
                contracts.definition,
                input_type=Params,
                file_fields=(),
                model_field="model_key" if expansion == "model_selection" else None,
                required_permissions=(),
            ),
        ),
    )
    client = request.getfixturevalue("client")
    settings = client.app.state.runtime.settings
    settings.flow_enabled = True
    login(client)
    final_input = {"text": "", "model_key": "chat"}
    if expansion == "model_selection":
        entry(client, channel(client))
        selection = query(client, lambda session: select_model(session, "chat"))
        final_input["model_key"] = selection.model_dump(mode="json")
    remaining = 65536 + extra_bytes - len(small_json(final_input))
    # Count UTF-8 bytes, including JSON structure, rather than Python characters.
    padding = "界" * (remaining // 3) + "x" * (remaining % 3)
    data = (
        {}
        if expansion == "default"
        else {"text": "expand" if expansion == "serializer" else padding}
    )
    payload = {"definition_key": contracts.definition.key, "request_key": "size", "input": data}
    response = client.post("/api/v1/flow-runs", json=payload)
    if extra_bytes:
        assert response.status_code == 422, response.text
        assert response.json()["code"] == "flow_value_too_large"

        async def verify(session):
            assert await session.scalar(select(FlowRun.id)) is None
            assert await session.scalar(select(FileReference.file_id)) is None
            log = await session.scalar(select(AuditLog).where(AuditLog.action == "flow.start"))
            assert log.outcome == "failure" and log.details == {"reason": "flow_value_too_large"}
            assert log.correlation_id == response.headers["x-request-id"]

        query(client, verify)
        payload["input"] = {"text": "small"}
        response = client.post("/api/v1/flow-runs", json=payload)
    assert response.status_code == 202, response.text
    if not extra_bytes:
        assert len(small_json(response.json()["input"])) == 65536
    assert client.post("/api/v1/flow-runs", json=payload).json()["id"] == response.json()["id"]


@pytest.mark.postgres
@pytest.mark.parametrize("case", ["invalid_uuid", "too_many"])
def test_flow_file_input_errors_are_422_without_runs_or_references(monkeypatch, request, case):
    from pydantic import ConfigDict, Field
    from support.flow_fixture import contracts

    from keystone.core.schemas import Input
    from keystone.platform.files.models import FileReference
    from keystone.platform.flow.models import FlowRun

    class FilesInput(Input):
        model_config = ConfigDict(serialize_by_alias=True)
        files: list[str] = Field(alias="fileIds")

    monkeypatch.setattr(
        composition,
        "FLOW_DEFINITIONS",
        (
            replace(
                contracts.definition,
                input_type=FilesInput,
                file_fields=("files",),
                model_field=None,
                required_permissions=(),
            ),
        ),
    )
    client = request.getfixturevalue("client")
    client.app.state.runtime.settings.flow_enabled = True
    login(client)
    identifiers = ["bad-id"] if case == "invalid_uuid" else [str(uuid4()) for _ in range(101)]
    payload = {
        "definition_key": contracts.definition.key,
        "request_key": "invalid-files",
        "input": {"fileIds": identifiers},
    }
    response = client.post("/api/v1/flow-runs", json=payload)
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "flow_invalid_input"

    async def verify(session):
        assert await session.scalar(select(FlowRun.id)) is None
        assert await session.scalar(select(FileReference.file_id)) is None
        log = await session.scalar(select(AuditLog).where(AuditLog.action == "flow.start"))
        assert log.outcome == "failure" and log.details == {"reason": "flow_invalid_input"}
        assert log.correlation_id == response.headers["x-request-id"]

    query(client, verify)
    # The rejected request has not occupied its idempotency key.
    payload["input"] = {"fileIds": []}
    retry = client.post("/api/v1/flow-runs", json=payload)
    assert retry.status_code == 202, retry.text
    assert retry.json()["input"] == {"files": []}


@pytest.mark.postgres
@pytest.mark.parametrize(
    "result,expected", [({"amount": "7"}, "succeeded"), ({"amount": 7}, "failed")]
)
def test_flow_reconciliation_uses_serialized_output_contract(
    monkeypatch, request, result, expected
):
    from pydantic import Field, field_serializer
    from support.flow_fixture import contracts

    from keystone.composition import execution_version
    from keystone.core.schemas import Input
    from keystone.platform.flow.tasks.workflows import reconcile_one

    class EmptyInput(Input):
        pass

    class Output(Input):
        amount: int = Field(alias="total")

        @field_serializer("amount")
        def serialize_amount(self, value) -> str:
            return str(value)

    monkeypatch.setattr(
        composition,
        "FLOW_DEFINITIONS",
        (
            replace(
                contracts.definition,
                input_type=EmptyInput,
                output_type=Output,
                file_fields=(),
                output_file_fields=(),
                model_field=None,
                required_permissions=(),
            ),
        ),
    )
    client = request.getfixturevalue("client")
    runtime = client.app.state.runtime
    runtime.settings.flow_enabled = True
    login(client)
    response = client.post(
        "/api/v1/flow-runs",
        json={"definition_key": contracts.definition.key, "request_key": "output", "input": {}},
    )
    assert response.status_code == 202, response.text
    run = response.json()
    advertised = client.get("/api/v1/flow-definitions").json()["items"][0]["output_schema"]
    assert advertised == run["definition_snapshot"]["output_schema"]

    class Engine:
        async def state(self, identifier):
            return {
                "status": "SUCCESS",
                "result": result,
                "version": execution_version(),
                "name": contracts.definition.entrypoint,
            }

    client.portal.call(reconcile_one, runtime, Engine(), UUID(run["id"]))
    done = client.get(f"/api/v1/flow-runs/{run['id']}").json()
    assert done["status"] == expected
    assert done["result"] == (result if expected == "succeeded" else None)
    assert done["error_code"] == (None if expected == "succeeded" else "flow_invalid_output")


@pytest.mark.postgres
def test_business_model_joins_shared_migration_metadata(client):
    from alembic import command
    from alembic.util.exc import AutogenerateDiffsDetected
    from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

    from keystone.core.migrations import migration_config
    from keystone.model_registry import load_models

    metadata = load_models()

    # A separate mapper registry keeps this test-only entity out of other test fixtures.
    class ProbeBase(DeclarativeBase):
        pass

    ProbeBase.metadata = metadata

    class Probe(ProbeBase):
        __tablename__ = "test_notes_probe"
        id: Mapped[int] = mapped_column(primary_key=True)

    try:
        config = migration_config(client.app.state.runtime.settings)
        with pytest.raises(AutogenerateDiffsDetected) as pending:
            command.check(config)
        assert len(pending.value.diffs) == 1
        assert pending.value.diffs[0][0] == "add_table"
        assert pending.value.diffs[0][1].fullname == Probe.__table__.fullname

        async def create_probe(session):
            connection = await session.connection()
            await connection.run_sync(Probe.__table__.create)
            session.add(Probe(id=1))

        query(client, create_probe)
        command.check(config)
    finally:
        metadata.remove(Probe.__table__)
        ProbeBase.registry.dispose()


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
    'personal_access_tokens', 'audit_logs', 'files', 'file_grants', 'file_references',
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


@pytest.fixture
def business_client(monkeypatch, request):
    install(monkeypatch)
    return request.getfixturevalue("client")


@pytest.mark.postgres
def test_business_role_pat_and_worker_authorization(business_client):
    client = business_client
    login(client)
    assert {READ, WRITE} <= set(client.get("/api/v1/auth/me").json()["permissions"])
    directory = client.get("/api/v1/permissions").json()
    assert {READ, WRITE} <= {item["key"] for item in directory}
    alice = create_user(client)
    reader = role(client, "reader", [READ])
    assert reader["permissions"] == [READ]
    assert assign(client, alice["id"], reader["id"]).status_code == 204
    roles = client.get("/api/v1/roles").json()
    assert {READ, WRITE} <= set(next(r for r in roles if r["key"] == "admin")["permissions"])
    assert (
        client.post(
            "/api/v1/roles", json={"key": "bad", "name": "bad", "permissions": ["other.x:read"]}
        ).json()["code"]
        == "unknown_permission"
    )

    login(client, "alice")
    client.post(
        "/api/v1/auth/password",
        json={
            "current_password": PASSWORD,
            "password": NEW_PASSWORD,
        },
    )
    login(client, "alice", NEW_PASSWORD)
    assert client.get("/api/v1/auth/me").json()["permissions"] == [READ]
    pat = token(client, [READ])
    assert client.get("/api/v1/auth/me", headers=bearer(pat["token"])).json()["permissions"] == [
        READ
    ]
    assert (
        client.post(
            "/api/v1/auth/tokens",
            json={
                "name": "too-wide",
                "permissions": [WRITE],
                "expires_at": pat["expires_at"],
            },
        ).json()["code"]
        == "permission_denied"
    )

    async def worker_grants(session):
        # Flow's trusted identity path uses the same catalog, retaining PAT restrictions.
        auth = await load_authorization(session, UUID(alice["id"]), pat_id=UUID(pat["id"]))
        return auth.permissions

    assert query(client, worker_grants) == {READ}
    login(client)
    assert (
        client.put(
            f"/api/v1/roles/{reader['id']}",
            json={
                "name": "reader",
                "permissions": [WRITE],
            },
        ).status_code
        == 200
    )
    assert query(client, worker_grants) == set()
    assert client.get("/api/v1/auth/me", headers=bearer(pat["token"])).json()["permissions"] == []


@pytest.mark.postgres
def test_two_applications_keep_catalogs_and_audit_actions_isolated(business_client, monkeypatch):
    first = business_client
    login(first)
    pat = token(first, [READ, "role:read"])
    monkeypatch.setattr(composition, "BUSINESS_CATALOGS", ())
    with TestClient(
        create_app(first.app.state.runtime.settings),
        backend_options={"loop_factory": new_event_loop},
    ) as second:
        login(second)
        assert READ not in {item["key"] for item in second.get("/api/v1/permissions").json()}
        assert READ in {item["key"] for item in first.get("/api/v1/permissions").json()}
        assert (
            READ
            not in second.get("/api/v1/auth/me", headers=bearer(pat["token"])).json()["permissions"]
        )
        assert (
            READ in first.get("/api/v1/auth/me", headers=bearer(pat["token"])).json()["permissions"]
        )

        async def record(session):
            await audit.record(session, ACTION, audit.Actor("system"), details={"status": "ok"})

        with pytest.raises(KeyError):
            query(second, record)
        query(first, record)


@pytest.mark.postgres
def test_business_audit_whitelist_transaction_and_identity(business_client):
    client = business_client
    user = login(client)
    pat = token(client, [WRITE])
    runtime = client.app.state.runtime
    context = AccessContext(
        runtime.database, runtime.settings, Credential("pat", pat["token"]), "test-business-request"
    )

    async def save(details, *, fail=False):
        async with context.access(
            WRITE, write=True, action=ACTION, target="test_notes.note", target_id="42"
        ) as (session, auth):
            # Test-only business write: no permanent entity or migration is introduced.
            await session.execute(text("CREATE TABLE keystone.test_notes_probe (id integer)"))
            await context.log(session, auth, ACTION, "test_notes.note", "42", details)
            if fail:
                raise ServiceError(409, "test_conflict", "Test rollback")

    with pytest.raises(ServiceError):
        client.portal.call(partial(save, {"after": {"status": "saved"}}, fail=True))

    async def rolled_back(session):
        assert await session.scalar(text("SELECT to_regclass('keystone.test_notes_probe')")) is None
        logs = (await session.scalars(select(AuditLog).where(AuditLog.action == ACTION))).all()
        assert [row.outcome for row in logs] == ["failure"]
        assert logs[0].actor_id == UUID(user["id"])
        assert logs[0].correlation_id == "test-business-request"
        assert logs[0].details == {"reason": "test_conflict", "credential_id": pat["id"]}

    query(client, rolled_back)
    for details in ({"after": {"password": "secret"}}, {"status": "x" * 8193}):
        with pytest.raises(ValueError):
            client.portal.call(save, details)
    client.portal.call(save, {"after": {"status": "saved"}})

    async def success(session):
        row = await session.scalar(
            select(AuditLog).where(
                AuditLog.action == ACTION,
                AuditLog.outcome == "success",
            )
        )
        assert row.actor_id == UUID(user["id"])
        assert row.correlation_id == "test-business-request"
        assert row.details == {"after": {"status": "saved"}, "credential_id": pat["id"]}

    query(client, success)
    limited = token(client, [READ])
    context = AccessContext(
        runtime.database,
        runtime.settings,
        Credential("pat", limited["token"]),
        "test-business-denied",
    )
    with pytest.raises(ServiceError, match="没有执行"):
        client.portal.call(save, {"status": "must-not-save"})

    async def denied(session):
        row = await session.scalar(
            select(AuditLog).where(
                AuditLog.action == ACTION,
                AuditLog.outcome == "denied",
            )
        )
        assert row.actor_id == UUID(user["id"])
        assert row.correlation_id == "test-business-denied"
        assert row.details == {"reason": "permission_denied", "credential_id": limited["id"]}

    query(client, denied)

    context = AccessContext(
        runtime.database,
        runtime.settings,
        Credential("pat", "invalid"),
        "test-business-unauthenticated",
    )
    with pytest.raises(ServiceError):
        client.portal.call(save, {"status": "must-not-save"})

    async def unauthenticated(session):
        row = await session.scalar(
            select(AuditLog).where(
                AuditLog.correlation_id == "test-business-unauthenticated",
            )
        )
        assert row.actor_type == "anonymous" and row.actor_id is None
        assert row.details == {"reason": "authentication_required"}

    query(client, unauthenticated)

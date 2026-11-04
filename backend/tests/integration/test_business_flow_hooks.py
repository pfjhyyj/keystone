"""Business entry and execution guards keep the platform's original authority checks."""

from uuid import UUID

import pytest
from sqlalchemy import select, text
from test_files_dictionary import upload
from test_flow import client as client
from test_flow import configuration as configuration
from test_flow import worker
from test_identity import bearer, query, token

from keystone.core.errors import ServiceError
from keystone.platform.flow.models import FlowRun
from keystone.platform.flow.schemas import Start
from keystone.platform.flow.services.artifacts import FlowArtifactService
from keystone.platform.flow.services.identity import ExecutionIdentity
from keystone.platform.identity.schemas.credentials import Credential
from keystone.platform.identity.services.access import AccessContext

pytestmark = pytest.mark.postgres


def test_business_entry_is_required_but_transactional_admission_remains_available(client):
    runtime = client.app.state.runtime
    definition = next(iter(runtime.flow_definitions.values()))
    file_id = upload(client).json()["id"]
    assert client.get("/api/v1/flow-definitions").status_code == 404
    assert client.post("/api/v1/flow-runs", json={}).status_code == 405
    assert query(client, lambda session: session.scalars(select(FlowRun))).all() == []
    personal = token(client, client.get("/api/v1/auth/me").json()["permissions"])
    identity = AccessContext(
        runtime.database, runtime.settings, Credential("pat", personal["token"])
    )
    data = Start(
        definition_key=definition.key,
        request_key="business-only",
        input={"source_file_id": file_id},
        model_bindings={"review": "chat"},
    )

    async def admit():
        async with identity.access() as (session, auth):
            return await runtime.flow_service(identity).start_in_transaction(session, auth, data)

    accepted = client.portal.call(admit)
    assert client.portal.call(admit)["id"] == accepted["id"]
    assert client.get(f"/api/v1/flow-runs/{accepted['id']}").status_code == 200


@pytest.mark.parametrize("operation", ["access", "artifacts"])
def test_execution_guard_rechecks_business_facts_and_cannot_replace_revoked_pat(client, operation):
    runtime = client.app.state.runtime
    personal = token(client, client.get("/api/v1/auth/me").json()["permissions"])
    file_id = upload(client).json()["id"]
    response = client.post(
        "/api/v1/_test/review",
        headers=bearer(personal["token"]),
        json={
            "definition_key": "document.review",
            "request_key": "guarded",
            "input": {"source_file_id": file_id},
            "model_bindings": {"review": "chat"},
        },
    )
    assert response.status_code == 202, response.text
    identifier = response.json()["id"]

    async def prepare(session):
        await session.execute(
            text("CREATE TABLE keystone.business_gate (allowed boolean NOT NULL)")
        )
        await session.execute(text("INSERT INTO keystone.business_gate VALUES (true)"))

    query(client, prepare)
    calls = []

    async def guard(session, auth, run_id, definition_key, inputs):
        assert run_id == UUID(identifier) and definition_key == "document.review"
        assert inputs["source_file_id"] == file_id and auth.kind == "pat"
        calls.append(run_id)
        if not await session.scalar(text("SELECT allowed FROM keystone.business_gate")):
            raise ServiceError(403, "business_rejected", "业务状态不允许继续")

    runtime.execution_guard = guard

    async def execute():
        execution = ExecutionIdentity(runtime, identifier)
        if operation == "access":
            async with execution.access():
                pass
        else:
            identity = AccessContext(
                runtime.database,
                runtime.settings,
                Credential("session", client.cookies.get("keystone_session")),
            )
            async with identity.access() as (session, auth):
                await FlowArtifactService(execution, runtime.storage).require_in_transaction(
                    session, auth, identifier, {}
                )

    client.portal.call(execute)
    query(
        client,
        lambda session: session.execute(text("UPDATE keystone.business_gate SET allowed = false")),
    )
    with pytest.raises(ServiceError) as denied:
        client.portal.call(execute)
    assert denied.value.code == "execution_not_authorized" and len(calls) == 2

    async def unavailable(*args):
        raise ServiceError(503, "business_dependency_unavailable", "依赖暂不可用")

    runtime.execution_guard = unavailable
    with pytest.raises(ServiceError) as deferred:
        client.portal.call(execute)
    assert deferred.value.code == "business_dependency_unavailable"
    runtime.execution_guard = guard
    assert client.delete(f"/api/v1/auth/tokens/{personal['id']}").status_code == 204
    with pytest.raises(ServiceError) as revoked:
        client.portal.call(execute)
    assert revoked.value.code == "execution_not_authorized" and len(calls) == 2


def test_worker_binds_services_with_its_admitted_runtime(client, tmp_path):
    marker = tmp_path / "runtime-binding.txt"
    code = f"""
from pathlib import Path
from keystone import composition
from support.flow_fixture.contracts import PACKAGE
composition.FLOW_PACKAGES = (PACKAGE,)
def bind(runtime):
    Path({str(marker)!r}).write_text(runtime.worker_identity.worker_id)
composition.WORKFLOW_BINDINGS = (bind,)
from keystone.entrypoints.worker import main
main()
"""
    with worker(client, tmp_path, code=code) as (process, log):
        from test_maintenance import wait_for

        wait_for(client, process, log, marker.exists)
        assert marker.read_text() == client.app.state.runtime.settings.worker_id

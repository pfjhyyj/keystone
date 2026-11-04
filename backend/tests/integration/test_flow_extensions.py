"""Real SQL/Worker guarantees for independent roles and collection files."""

import json
from dataclasses import replace
from uuid import UUID

import pytest
from sqlalchemy import select
from support.flow_engine import flow_engine as flow_engine
from support.flow_packages import install_package
from support.multi_role_flow import PACKAGE, definition
from test_files_dictionary import upload
from test_flow import client as client
from test_flow import configuration as configuration
from test_flow import wait_run, worker
from test_identity import query
from test_model_access import channel, entry

from keystone.platform.files.models import FileReference
from keystone.platform.flow.models import FlowRun
from keystone.platform.model_access.models import ModelInvocation

pytestmark = [pytest.mark.postgres, pytest.mark.usefixtures("flow_engine")]


def payload(file_id, key="multi"):
    return {
        "definition_key": definition.key,
        "definition_revision": "1",
        "request_key": key,
        "input": {"documents": [{"file_id": file_id}]},
        "model_bindings": {"analyze": "chat", "review": "chat"},
    }


def install(client, monkeypatch):
    install_package(client, monkeypatch, PACKAGE, engine=False)


def worker_code(request_log, crash=None):
    return f"""import json, os
from pathlib import Path
import httpx2
from keystone import composition
from support import multi_role_flow
composition.FLOW_PACKAGES = (multi_role_flow.PACKAGE,)
composition.execution_version.cache_clear()
from keystone.platform.model_access.adapters.http import HttpClient
original_transport = HttpClient.transport
def transport(self, provider):
    underlying = original_transport(self, provider)
    async def send(request):
        if request.method == 'POST':
            with Path({str(request_log)!r}).open('a') as log:
                log.write(json.dumps({{'id': request.headers['x-keystone-invocation-id'],
                                      'model': json.loads(request.content)['model']}}) + '\\n')
            if {crash!r} == 'registered':
                os._exit(71)
        return await underlying.handle_async_request(request)
    return httpx2.MockTransport(send)
HttpClient.transport = transport
if {crash!r} == 'response':
    from keystone.platform.flow.workflows.context import FlowContext
    original_invoke = FlowContext.invoke_text
    async def invoke(self, *args, **kwargs):
        await original_invoke(self, *args, **kwargs)
        os._exit(71)
    FlowContext.invoke_text = invoke
if {crash!r} == 'checkpoint':
    multi_role_flow.after_generate = lambda *args: os._exit(71)
from keystone.entrypoints.worker import main
main()
"""


def bind_models(client, data, distinct):
    if distinct:
        second = entry(
            client,
            channel(client, name="review-channel"),
            key="review-chat",
            provider_model="mock-review",
        )
        data["model_bindings"]["review"] = second["key"]
    return data


def assert_routing(client, request_log, distinct, roles=("analyze", "review")):
    calls = query(client, lambda s: s.scalars(select(ModelInvocation))).all()
    by_role = {c.flow_context["model_role"]: c for c in calls}
    assert set(by_role) == set(roles) and len(calls) == len(roles)
    sent = [json.loads(line) for line in request_log.read_text().splitlines()]
    assert {item["id"] for item in sent} == {str(c.id) for c in calls}
    assert len(sent) == len(calls)
    for item in sent:
        assert item["model"] == next(c.provider_model for c in calls if str(c.id) == item["id"])
    assert by_role["analyze"].provider_model == "mock-chat"
    if "review" in roles:
        assert by_role["review"].provider_model == ("mock-review" if distinct else "mock-chat")
        for field in ("model_entry_id", "channel_id"):
            assert (
                getattr(by_role["analyze"], field) != getattr(by_role["review"], field)
            ) == distinct
    return by_role


@pytest.mark.parametrize("distinct", [False, True])
def test_roles_and_array_outputs_in_real_worker(client, tmp_path, monkeypatch, distinct):
    install(client, monkeypatch)
    file_id = upload(client, b"Review this text").json()["id"]
    data = bind_models(client, payload(file_id), distinct)
    accepted = client.post("/api/v1/_test/review", json=data)
    assert accepted.status_code == 202, accepted.text
    run = accepted.json()
    assert run["model_bindings"] == data["model_bindings"]
    assert "selection" not in str(run["definition_snapshot"])
    request_log = tmp_path / "requests.jsonl"
    with worker(client, tmp_path, code=worker_code(request_log)) as (process, log):
        done = wait_run(client, run["id"], process, log)
        assert done["status"] == "succeeded", (done, log.read_text())
    assert_routing(client, request_log, distinct)
    stored = query(client, lambda s: s.get(FlowRun, UUID(run["id"])))
    assert stored.definition_snapshot["package_key"] == PACKAGE.key
    refs = query(
        client,
        lambda s: s.scalars(
            select(FileReference).where(FileReference.owner == f"flow:{run['id']}")
        ),
    ).all()
    assert len({r.file_id for r in refs if r.slot == "output"}) == 2
    assert client.post("/api/v1/_test/review", json=data).json()["id"] == run["id"]


@pytest.mark.parametrize("crash", ["registered", "response", "checkpoint"])
def test_direct_text_crash_recovery_never_resends(client, tmp_path, monkeypatch, crash):
    install(client, monkeypatch)
    file_id = upload(client, b"Recover this text").json()["id"]
    data = bind_models(client, payload(file_id), True)
    response = client.post("/api/v1/_test/review", json=data)
    assert response.status_code == 202, response.text
    run_id = response.json()["id"]
    request_log = tmp_path / "requests.jsonl"
    with worker(client, tmp_path, code=worker_code(request_log, crash)) as (process, log):
        assert process.wait(timeout=35) == 71, log.read_text()
    before = assert_routing(client, request_log, True, ("analyze",))["analyze"]
    assert before.status == ("running" if crash == "registered" else "succeeded")
    with worker(client, tmp_path, code=worker_code(request_log)) as (process, log):
        done = wait_run(client, run_id, process, log)
        assert done["status"] == ("succeeded" if crash == "checkpoint" else "failed"), (
            done,
            log.read_text(),
        )
    roles = ("analyze", "review") if crash == "checkpoint" else ("analyze",)
    after = assert_routing(client, request_log, True, roles)["analyze"]
    assert before.id == after.id
    assert after.status == ("unknown" if crash == "registered" else "succeeded")
    if crash != "registered":
        assert after.usage == before.usage
        assert after.receipt_ids == before.receipt_ids
        assert after.cost_amount == before.cost_amount
    if crash != "checkpoint":
        assert done["error_code"] == "model_result_unavailable"
    refs = query(
        client,
        lambda s: s.scalars(
            select(FileReference).where(
                FileReference.owner == f"flow:{run_id}", FileReference.slot == "output"
            )
        ),
    ).all()
    assert len(refs) == (2 if crash == "checkpoint" else 0)


def test_bad_roles_rollback_and_idempotency_does_not_resolve_new_defaults(client, monkeypatch):
    install(client, monkeypatch)
    file_id = upload(client).json()["id"]
    data = payload(file_id)
    for bindings in ({"analyze": "chat"}, {"analyze": "chat", "review": "missing"}):
        response = client.post("/api/v1/_test/review", json={**data, "model_bindings": bindings})
        assert response.status_code in {409, 422}, response.text
        assert query(client, lambda s: s.scalar(select(FlowRun.id))) is None
        assert (
            query(
                client,
                lambda s: s.scalar(
                    select(FileReference.file_id).where(FileReference.owner.like("flow:%"))
                ),
            )
            is None
        )
    accepted = client.post("/api/v1/_test/review", json=data).json()
    client.app.state.runtime.flow_definitions = {definition.key: replace(definition, revision="2")}
    assert client.post("/api/v1/_test/review", json=data).json()["id"] == accepted["id"]
    assert (
        client.post("/api/v1/_test/review", json={**data, "definition_revision": "2"}).status_code
        == 409
    )
    assert (
        client.post("/api/v1/_test/review", json={**data, "request_key": "new"}).status_code == 409
    )

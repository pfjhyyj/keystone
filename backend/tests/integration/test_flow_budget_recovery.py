"""Real Worker requests/usage, checkpoint recovery and shared role budgets."""

import json
from uuid import UUID

import pytest
from sqlalchemy import select
from support.budget_flow import (
    PACKAGE,
    definition,
)
from support.flow_packages import assert_correlated, install_package
from test_files_dictionary import client as client
from test_flow import wait_run, worker
from test_identity import query
from test_model_access import channel, entry
from test_model_access import model_configuration as model_configuration

from keystone.platform.flow.models import FlowBudgetClaim, FlowRunBudget
from keystone.platform.model_access.models import ModelDispatchScope, ModelInvocation

pytestmark = pytest.mark.postgres


def accept(client, monkeypatch, definition_key, attempts, *, engine=True):
    install_package(client, monkeypatch, PACKAGE, engine=engine)
    entry(client, channel(client), key="first", provider_model="first-model")
    entry(client, channel(client, name="second"), key="second", provider_model="second-model")
    response = client.post(
        "/api/v1/_test/review",
        json={
            "definition_key": definition_key,
            "request_key": "budget-recovery",
            "input": {"attempts": attempts},
            "model_bindings": {"first": "first", "second": "second"},
        },
    )
    assert response.status_code == 202, response.text
    return response.json()["id"]


def code(path, fault=None):
    return f"""import json, os
from pathlib import Path
import httpx2
from keystone import composition
from support import budget_flow
composition.FLOW_PACKAGES = (budget_flow.PACKAGE,)
composition.execution_version.cache_clear()
from keystone.platform.model_access.adapters.http import HttpClient
original = HttpClient.transport
def transport(self, provider):
    source = original(self, provider)
    async def send(request):
        if request.method == 'POST':
            with Path({str(path)!r}).open('a') as log:
                log.write(json.dumps({{'id': request.headers['X-Keystone-Invocation-Id'],
                    'model': json.loads(request.content)['model']}}) + '\\n')
            if {fault!r} == 'registered':
                os._exit(71)
        response = await source.handle_async_request(request)
        if request.method == 'POST' and {fault!r} == 'usage':
            data = response.json()
            data['usage'].pop('prompt_tokens', None)
            return httpx2.Response(200, headers=response.headers, json=data)
        return response
    return httpx2.MockTransport(send)
HttpClient.transport = transport
if {fault!r} == 'checkpoint':
    def after_checkpoint(run_id, index):
        if index == 0:
            os._exit(71)
    budget_flow.after_checkpoint = after_checkpoint
from keystone.entrypoints.worker import main
main()
"""


@pytest.mark.parametrize(
    "case,key,attempts,calls",
    [
        ("normal", definition.key, 2, 2),
        ("requests", definition.key, 3, 2),
        ("checkpoint", definition.key, 3, 2),
        ("registered", definition.key, 2, 1),
    ],
)
def test_real_worker_budget_facts_survive_checkpoint_and_unknown_result(
    client, monkeypatch, tmp_path, case, key, attempts, calls
):
    run_id = accept(client, monkeypatch, key, attempts)
    request_log = tmp_path / "requests.jsonl"
    before = None
    if case in {"checkpoint", "registered"}:
        with worker(client, tmp_path, code=code(request_log, case)) as (process, log):
            assert process.wait(timeout=30) == 71, log.read_text()
        before = query(client, lambda s: s.get(FlowRunBudget, UUID(run_id)))
        assert before.counters["model_requests"] == 1
    with worker(client, tmp_path, code=code(request_log)) as (process, log):
        done = wait_run(client, run_id, process, log)
    assert done["status"] == ("succeeded" if case == "normal" else "failed"), done
    if case != "normal":
        assert done["error_code"] == (
            "model_result_unavailable" if case == "registered" else "flow_budget_exceeded"
        ), done
    budget = query(client, lambda s: s.get(FlowRunBudget, UUID(run_id)))
    invocations = query(client, lambda s: s.scalars(select(ModelInvocation))).all()
    assert len(invocations) == calls
    assert_correlated(client, run_id, invocations)
    sent = (
        [json.loads(line) for line in request_log.read_text().splitlines()]
        if request_log.exists()
        else []
    )
    assert len(sent) == calls and len({row["id"] for row in sent}) == calls
    assert budget.counters["model_requests"] == calls
    assert budget.counters["output_tokens"] == calls * 16
    assert budget.counters["unreported_text_requests"] == int(case == "registered")
    assert budget.counters["input_tokens"] == (0 if case == "registered" else calls * 12)
    if calls == 2:
        assert {row.flow_context["model_role"] for row in invocations} == {"first", "second"}
        assert {row["model"] for row in sent} == {"first-model", "second-model"}
    if case == "registered":
        assert invocations[0].status == "unknown" and budget.counters == before.counters
    else:
        assert all(
            row.status == "succeeded" and row.cost_source == "response" for row in invocations
        )
    claims = query(client, lambda s: s.scalars(select(FlowBudgetClaim))).all()
    assert len([row for row in claims if row.kind == "model"]) == calls


def test_missing_budget_of_a_new_snapshot_cannot_fall_back_to_unlimited(
    client, monkeypatch, tmp_path
):
    from sqlalchemy import delete

    run_id = accept(client, monkeypatch, definition.key, 1)

    async def corrupt(session):
        await session.execute(delete(FlowRunBudget).where(FlowRunBudget.run_id == UUID(run_id)))

    query(client, corrupt)
    request_log = tmp_path / "requests.jsonl"
    with worker(client, tmp_path, code=code(request_log)) as (process, log):
        done = wait_run(client, run_id, process, log)
    assert done["status"] == "failed" and done["error_code"] == "flow_budget_unavailable", done
    assert not request_log.exists()
    assert query(client, lambda s: s.scalar(select(ModelInvocation.id))) is None
    assert query(client, lambda s: s.scalar(select(ModelDispatchScope.scope))) is None

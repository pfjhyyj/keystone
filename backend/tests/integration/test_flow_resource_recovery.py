"""Native Step/Agent restarts retain one cumulative tool, iteration or artifact allowance."""

from uuid import UUID

import pytest
from sqlalchemy import select
from support.flow_packages import assert_correlated, install_package
from support.resource_flow import (
    PACKAGE,
    agent_definition,
    artifact_definition,
    iteration_definition,
    tool_definition,
    workspace_definition,
)
from test_files_dictionary import client as client
from test_flow import wait_run, worker
from test_identity import query
from test_model_access import channel, entry
from test_model_access import model_configuration as model_configuration

from keystone.platform.files.models import File
from keystone.platform.flow.models import FlowBudgetClaim, FlowRunBudget
from keystone.platform.model_access.models import ModelInvocation

pytestmark = pytest.mark.postgres


def accept(client, monkeypatch, kind):
    definition = {
        "tool": tool_definition,
        "iteration": iteration_definition,
        "artifact": artifact_definition,
        "agents": agent_definition,
        "workspace": workspace_definition,
    }[kind]
    install_package(client, monkeypatch, PACKAGE)
    bindings = {}
    if kind == "agents":
        entry(client, channel(client))
        bindings = {"review": "chat"}
    response = client.post(
        "/api/v1/_test/review",
        json={
            "definition_key": definition.key,
            "request_key": "resource-recovery",
            "input": {"attempts": 2},
            "model_bindings": bindings,
        },
    )
    assert response.status_code == 202, response.text
    return UUID(response.json()["id"])


def code(kind, crash=None):
    return f"""import os
from keystone import composition
from support import resource_flow
composition.FLOW_PACKAGES = (resource_flow.PACKAGE,)
composition.execution_version.cache_clear()
if {crash!r} == 'reserved':
    def after_reserved(run_id, kind, index):
        if kind == {kind!r} and index == 0:
            os._exit(71)
    resource_flow.after_reserved = after_reserved
    if {kind!r} == 'artifact':
        from keystone.platform.flow.services.budgets import FlowBudgetService
        original = FlowBudgetService.reserve_artifact
        async def reserve(self, *args, **kwargs):
            await original(self, *args, **kwargs)
            os._exit(71)
        FlowBudgetService.reserve_artifact = reserve
if {crash!r} == 'checkpoint':
    def after_checkpoint(run_id, kind, index):
        if kind == {kind!r} and index == 0:
            os._exit(71)
    resource_flow.after_checkpoint = after_checkpoint
if {crash!r} == 'uploaded':
    from keystone.platform.flow.workflows.context import FlowContext
    original = FlowContext.upload
    async def upload(self, *args, **kwargs):
        await original(self, *args, **kwargs)
        os._exit(71)
    FlowContext.upload = upload
from keystone.entrypoints.worker import main
main()
"""


@pytest.mark.parametrize(
    "kind,crash",
    [
        ("tool", "reserved"),
        ("tool", "checkpoint"),
        ("artifact", "reserved"),
        ("artifact", "uploaded"),
        ("artifact", "checkpoint"),
        ("agents", "checkpoint"),
    ],
)
def test_resource_allowances_survive_real_worker_restart(
    client, monkeypatch, tmp_path, kind, crash
):
    run_id = accept(client, monkeypatch, kind)
    with worker(client, tmp_path, code=code(kind, crash)) as (process, log):
        assert process.wait(timeout=35) == 71, log.read_text()
    before = query(client, lambda s: s.get(FlowRunBudget, run_id))
    assert (
        before.counters[
            {
                "tool": "tool_calls",
                "iteration": "iterations",
                "artifact": "file_count",
                "agents": "tool_calls",
            }[kind]
        ]
        == 1
    )
    before_files = query(client, lambda s: s.scalars(select(File))).all()
    with worker(client, tmp_path, code=code(kind)) as (process, log):
        done = wait_run(client, str(run_id), process, log)
        assert done["status"] == "failed" and done["error_code"] == "flow_budget_exceeded", (
            done,
            log.read_text(),
        )
    current = query(client, lambda s: s.get(FlowRunBudget, run_id))
    counter = {
        "tool": "tool_calls",
        "iteration": "iterations",
        "artifact": "file_count",
        "agents": "tool_calls",
    }[kind]
    assert current.counters[counter] == 1
    claim_kind = "file" if kind == "artifact" else "tool" if kind == "agents" else kind
    claims = query(
        client,
        lambda s: s.scalars(select(FlowBudgetClaim).where(FlowBudgetClaim.kind == claim_kind)),
    ).all()
    assert len(claims) == 1
    if kind == "artifact":
        files = query(client, lambda s: s.scalars(select(File))).all()
        assert (
            len(files) == 1 and files[0].status == "active" and current.counters["file_bytes"] == 4
        )
        if before_files:
            assert files[0].id == before_files[0].id
    if kind == "agents":
        calls = query(client, lambda s: s.scalars(select(ModelInvocation))).all()
        assert (
            len(calls) == current.counters["model_requests"] == current.counters["iterations"] == 3
        )
        assert len({row.flow_context["agent_call_id"] for row in calls}) == 2
        assert all(row.status == "succeeded" for row in calls)
        assert_correlated(client, run_id, calls)


def test_full_workspace_and_single_run_permit_are_reclaimed_after_process_exit(
    client, monkeypatch, tmp_path
):
    run_id = accept(client, monkeypatch, "workspace")
    with worker(client, tmp_path, code=code("workspace", "reserved")) as (process, log):
        assert process.wait(timeout=35) == 71, log.read_text()
    root = client.app.state.runtime.storage.coordination.root
    assert list((root / ".capacity").glob("*.json"))
    assert list((root / ".transfers").iterdir())
    with worker(client, tmp_path, code=code("workspace")) as (process, log):
        done = wait_run(client, str(run_id), process, log)
        assert done["status"] == "succeeded", (done, log.read_text())
    assert not list((root / ".capacity").iterdir()) and not list((root / ".transfers").iterdir())
    budget = query(client, lambda s: s.get(FlowRunBudget, run_id))
    assert budget.counters["file_count"] == 1 and budget.counters["file_bytes"] == 4
    assert len(query(client, lambda s: s.scalars(select(File))).all()) == 1

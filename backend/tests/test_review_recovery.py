"""Crash after durable model checkpoints: replay must reconstruct every budget."""

import time
from dataclasses import replace

import pytest
from test_files_dictionary import upload
from test_flow import client as client
from test_flow import configuration as configuration
from test_flow import start, wait_run, worker

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize(
    "budget", ["requests", "tool_calls", "total_tokens", "output_retries", "deadline"]
)
def test_completed_checkpoints_do_not_reset_budgets(client, tmp_path, budget):
    if budget == "deadline":
        runtime = client.app.state.runtime
        definition = runtime.flow_definitions["document.review"]
        runtime.flow_definitions = {definition.key: replace(definition, timeout_seconds=3)}
    file_id = upload(client, b"Review this source.").json()["id"]
    run = start(client, file_id).json()
    configuration_code = (
        f"BUDGET_KIND = {budget!r}\n"
        + """
from dataclasses import replace
from pydantic_ai.messages import ToolCallPart
from keystone import composition
from keystone.examples.document_review import workflow
from keystone.platform.flow.agent_model import FlowModel

limits = {'requests': 2} if BUDGET_KIND == 'requests' else {}
if BUDGET_KIND == 'tool_calls':
    limits = {'tool_calls': 1}
if BUDGET_KIND == 'total_tokens':
    limits = {'total_tokens': 30}  # Each checkpointed mock response reports 20.
if BUDGET_KIND == 'deadline':
    limits = {'seconds': 3}
    composition.FLOW_DEFINITIONS = (replace(workflow.definition, timeout_seconds=3),)
workflow.BUDGET = replace(workflow.BUDGET, **limits)

original_request = FlowModel.request
async def edited_request(self, *args, **kwargs):
    response = await original_request(self, *args, **kwargs)
    function = next(part for part in response.parts if isinstance(part, ToolCallPart))
    if function.tool_name == 'submit_review':
        if BUDGET_KIND == 'output_retries':
            values = function.args_as_dict()
            function.args = {**values, 'characters': values['characters'] + 1}
        else:
            function.tool_name, function.args = 'inspect_document', {}
    return response
FlowModel.request = edited_request
"""
    )
    crash_code = """
import os
from pydantic_ai._agent_graph import ModelRequestNode
append_response = ModelRequestNode._append_response
checkpointed = 3 if BUDGET_KIND == 'output_retries' else (1 if BUDGET_KIND == 'deadline' else 2)
seen = 0

def crash_after_checkpoint(ctx, response):
    # The DBOS model step has returned its persisted response; graph accounting has
    # not yet consumed it. This is deliberately different from crashing in the SDK.
    global seen
    seen += 1
    if seen == checkpointed:
        os._exit(71)
    append_response(ctx, response)
ModelRequestNode._append_response = staticmethod(crash_after_checkpoint)
"""
    main = "\nfrom keystone.entrypoints.worker import main; main()"
    with worker(client, tmp_path, code=configuration_code + crash_code + main) as (process, log):
        assert process.wait(timeout=30) == 71, log.read_text(encoding="utf8")
    before = client.get(f"/api/v1/model-invocations?flow_run_id={run['id']}").json()
    expected = 3 if budget == "output_retries" else (1 if budget == "deadline" else 2)
    assert len(before) == expected and all(row["status"] == "succeeded" for row in before)
    if budget == "deadline":
        time.sleep(3.1)
    with worker(client, tmp_path, code=configuration_code + main) as (process, log):
        done = wait_run(client, run["id"], process, log)
    if budget == "deadline":
        assert done["status"] == "cancelled", done
    else:
        expected_error = (
            "flow_invalid_output" if budget == "output_retries" else "flow_budget_exceeded"
        )
        assert (done["status"], done["error_code"]) == ("failed", expected_error), done
    after = client.get(f"/api/v1/model-invocations?flow_run_id={run['id']}").json()
    assert {row["id"] for row in after} == {row["id"] for row in before}
    assert done["result"] is None
    assert len(client.get("/api/v1/files").json()) == 1

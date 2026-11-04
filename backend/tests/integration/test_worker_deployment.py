"""Offline deployment gates and read-only diagnostics, never a business database."""

import json

import pytest
from support.flow_engine import flow_engine as flow_engine
from test_files_dictionary import client as client
from test_flow import configuration as configuration

from keystone.core.execution.runtime import inspect_queues

pytestmark = [pytest.mark.postgres, pytest.mark.usefixtures("flow_engine")]


def test_queue_inspection_never_launches_or_exposes_workflow_input(client):
    runtime = client.app.state.runtime
    engine = runtime.flow_engine.client

    async def prepare():
        await engine.enqueue_async(
            {
                "workflow_name": "unregistered-test",
                "queue_name": "keystone-system",
                "workflow_id": "inspect-only",
                "app_version": "old-release",
            },
            "private-inspection-input",
        )

    client.portal.call(prepare)
    result = client.portal.call(inspect_queues, runtime.settings)
    assert result["status"] == "ok" and not result["truncated"]
    assert result["active"] == [
        {
            "queue": "keystone-system",
            "worker_id": None,
            "execution_version": "old-release",
            "state": "ENQUEUED",
            "count": 1,
        }
    ]
    assert "private-inspection-input" not in json.dumps(result)

    async def count():
        return len(await engine.list_workflows_async(limit=10, load_input=False, load_output=False))

    assert client.portal.call(count) == 1

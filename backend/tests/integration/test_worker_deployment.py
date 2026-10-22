"""Offline deployment gates and read-only diagnostics, never a business database."""

import json
from functools import partial
from uuid import UUID, uuid4

import pytest
from sqlalchemy import update
from test_files_dictionary import client as client
from test_flow import configuration as configuration
from test_identity import query
from test_model_access import invoke

from keystone.composition import execution_version
from keystone.core.errors import ServiceError
from keystone.core.execution.runtime import inspect_queues
from keystone.core.worker_identity import WorkerIdentity
from keystone.core.worker_registration import identity_lease
from keystone.entrypoints.cli import recover_model_calls
from keystone.platform.model_access.models import ModelInvocation
from keystone.platform.model_access.services.recovery import recover_interrupted_calls

pytestmark = pytest.mark.postgres


def test_legacy_recovery_requires_all_processes_stopped_and_preserves_api_calls(client):
    runtime = client.app.state.runtime
    legacy, ordinary = [invoke(client).json()["invocation_id"] for _ in range(2)]
    original = client.get(f"/api/v1/model-invocations/{legacy}").json()

    async def legacy_rows(session):
        await session.execute(update(ModelInvocation).values(status="running", finished_at=None))
        await session.execute(
            update(ModelInvocation)
            .where(ModelInvocation.id == UUID(legacy))
            .values(flow_context={"flow_run_id": str(uuid4())})
        )

    query(client, legacy_rows)
    owner = WorkerIdentity("keystone-worker", uuid4(), execution_version())

    async def recover():
        async with identity_lease(runtime.database, runtime.storage, owner.worker_id) as lease:
            return await recover_interrupted_calls(runtime.database, owner, lease=lease)

    with pytest.raises(ServiceError) as blocked:
        client.portal.call(recover)
    assert blocked.value.code == "legacy_flow_recovery_required"
    recovery = partial(
        recover_model_calls,
        runtime.settings,
        api_stopped=True,
        legacy_flow=True,
        workers_stopped=True,
    )
    with runtime.storage.lease("keystone-flow-worker", shared=True):
        with pytest.raises(ServiceError):
            client.portal.call(recovery)
    assert query(client, lambda s: s.get(ModelInvocation, UUID(legacy))).status == "running"
    assert client.portal.call(recovery) == 1
    result = client.get(f"/api/v1/model-invocations/{legacy}").json()
    assert result["status"] == "unknown" and result["cost_amount"] == original["cost_amount"]
    assert query(client, lambda s: s.get(ModelInvocation, UUID(ordinary))).status == "running"
    assert client.portal.call(recovery) == 0
    audits = client.get("/api/v1/audit-logs?action=model.invocation.recover").json()
    assert len(audits) == 1 and audits[0]["details"]["source"] == "offline_flow_recovery"


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

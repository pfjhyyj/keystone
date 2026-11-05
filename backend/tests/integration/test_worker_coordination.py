"""Shared Worker capacity and connection ownership."""

import asyncio
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import text
from support.flow_engine import flow_engine as flow_engine
from test_files_dictionary import client as client
from test_flow import configuration as configuration
from test_flow import worker
from test_identity import token
from test_maintenance import wait_for
from test_model_access import internal_service

from keystone.core.errors import ServiceError
from keystone.platform.audit.services import writer as audit
from keystone.platform.model_access.schemas import Invoke
from keystone.platform.model_access.services.runtime import ModelRuntime

pytestmark = [pytest.mark.postgres, pytest.mark.usefixtures("flow_engine")]


def test_large_global_inference_budget_does_not_exhaust_registration_connections(
    client, monkeypatch
):
    runtime = client.app.state.runtime
    runtime.settings.model_inference_concurrency = 32
    runtime.settings.model_scope_concurrency = 16
    coordination = runtime.models.coordination
    original = coordination.inference
    active = peak = 0

    @asynccontextmanager
    async def simultaneous(scope):
        nonlocal active, peak
        async with original(scope) as lease:
            active += 1
            peak = max(peak, active)
            try:
                # Let calls hold permits before requesting registration connections.
                await asyncio.sleep(0.5)
                yield lease
            finally:
                active -= 1

    monkeypatch.setattr(coordination, "inference", simultaneous)
    model = ModelRuntime(internal_service(client, token(client, ["model:invoke"])["token"]))
    data = Invoke(model_key="chat", messages=[{"role": "user", "content": "local fixture"}])

    async def load():
        async with asyncio.timeout(5):
            results = await asyncio.gather(
                *(model.invoke(data) for _ in range(12)), return_exceptions=True
            )
        completed = [r for r in results if isinstance(r, dict)]
        assert completed
        assert all(
            isinstance(r, dict) or isinstance(r, ServiceError) and r.code == "model_capacity_busy"
            for r in results
        ), [(type(r).__name__, getattr(r, "code", None)) for r in results]
        return len(completed)

    completed = client.portal.call(load)
    assert peak <= 4
    rows = client.get("/api/v1/model-invocations").json()
    assert len(rows) == completed and all(r["status"] == "succeeded" for r in rows)


def test_persisted_limits_are_correct_before_executor_launch(client, tmp_path):
    runtime = client.app.state.runtime
    runtime.settings.flow_worker_concurrency = runtime.settings.flow_global_concurrency = 1
    names = [
        "keystone-flows",
        "keystone-system",
        "keystone-files-maintenance",
        "keystone-flow-maintenance",
        "keystone-model-maintenance",
    ]

    async def stale():
        for name in names:
            await runtime.flow_engine.client.register_queue_async(
                name, global_concurrency=8, worker_concurrency=8
            )

    client.portal.call(stale)
    code = """
from dbos import DBOS, DBOSClient
from keystone.core.config import Settings
original = DBOS.launch.__func__
@classmethod
def launch(cls):
    client = DBOSClient(system_database_url=Settings().database_url.get_secret_value(),
                        application_name='keystone', lazy=True)
    try:
        queues = {q.name:q for q in client.list_queues()}
        for name in ('keystone-flows','keystone-files-maintenance',
                     'keystone-flow-maintenance','keystone-model-maintenance'):
            assert queues[name]._concurrency == queues[name]._worker_concurrency == 1
        assert queues['keystone-system']._concurrency == 2
        print('queue_limits_prepared', flush=True)
    finally: client.destroy()
    return original(cls)
DBOS.launch = launch
from keystone.entrypoints.worker import main
main()
"""
    with worker(client, tmp_path, code=code) as (process, log):
        wait_for(
            client,
            process,
            log,
            lambda: client.get("/api/v1/worker/status").json()["status"] == "ready",
        )
        assert "queue_limits_prepared" in log.read_text(encoding="utf8")


def test_balance_cannot_commit_after_its_scope_connection_is_lost(client, monkeypatch):
    runtime = client.app.state.runtime
    identifier = client.get("/api/v1/model-channels").json()[0]["id"]
    endpoint = f"/api/v1/model-channels/{identifier}/balance"
    original = client.post(endpoint).json()["balance_snapshot"]
    leases = []
    scope = runtime.models.coordination.scope

    @asynccontextmanager
    async def captured(identifier):
        async with scope(identifier) as lease:
            leases.append(lease)
            yield lease

    async def balance(*args):
        return {
            "scope": "account",
            "items": [{"kind": "balance", "amount": "900", "unit": "credits"}],
        }

    record = audit.record
    terminated = False

    async def interrupted(session, action, *args, **kwargs):
        nonlocal terminated
        result = await record(session, action, *args, **kwargs)
        if action == "model.balance.refresh" and not terminated:
            terminated = True
            async with runtime.database.transaction() as other:
                assert await other.scalar(
                    text("SELECT pg_terminate_backend(:pid)"), {"pid": leases[-1].pid}
                )
        return result

    monkeypatch.setattr(runtime.models.coordination, "scope", captured)
    monkeypatch.setattr(runtime.models.billing, "balance", balance)
    monkeypatch.setattr(audit, "record", interrupted)
    response = client.post(endpoint)
    assert response.status_code != 200
    assert client.get("/api/v1/model-channels").json()[0]["balance_snapshot"] == original

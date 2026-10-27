"""Two real DBOS processes, isolated PostgreSQL, local provider fixtures only."""

import json
import time
from contextlib import asynccontextmanager
from uuid import UUID

import pytest
from sqlalchemy import select
from support.flow_engine import flow_engine as flow_engine
from support.processes import stop_process
from test_auto_costs import billing as billing
from test_auto_costs import call, configure, due, process, receipt, stored, view
from test_files_dictionary import client as client
from test_files_dictionary import upload
from test_flow import configuration as configuration
from test_flow import start, wait_run, worker
from test_identity import query
from test_maintenance import schedules, wait_for

from keystone.core.errors import ServiceError
from keystone.platform.model_access.adapters.coordination import QueryCoordination
from keystone.platform.model_access.models import Channel
from keystone.platform.model_access.tasks import workflows as model_costs

pytestmark = [pytest.mark.postgres, pytest.mark.usefixtures("flow_engine")]
FIRST, SECOND = "keystone-worker", "keystone-worker-02"


@pytest.fixture
def pair(client):
    settings = client.app.state.runtime.settings
    settings.flow_worker_concurrency = 1
    return settings


def kill(process):
    stop_process(process)


def status(client):
    return client.get("/api/v1/worker/status").json()


def test_shared_plans_limits_idle_health_and_duplicate_identity(client, tmp_path, pair):
    with (
        worker(client, tmp_path, fast_health=False) as (a, la),
        worker(client, tmp_path, worker_id=SECOND, fast_health=False) as (b, lb),
    ):
        wait_for(
            client,
            a,
            la,
            lambda: status(client)["status"] == "ready" and status(client)["ready_workers"] == 2,
        )
        assert b.poll() is None, lb.read_text(encoding="utf8")
        current = status(client)
        assert current["ready_workers"] == current["expected_workers"] == 2
        assert len(schedules(client)) == 3
        identity = {row["worker_id"]: row["instance_id"] for row in current["instances"]}
        with worker(client, tmp_path) as (duplicate, log):
            duplicate.wait(timeout=15)
            assert duplicate.returncode != 0
            assert {
                row["worker_id"]: row["instance_id"] for row in status(client)["instances"]
            } == identity

        async def queues():
            return await client.app.state.runtime.flow_engine.client.list_queues_async()

        queues_by_name = {q.name: q for q in client.portal.call(queues)}
        assert queues_by_name[model_costs.QUERY_QUEUE]._concurrency == 2
        assert queues_by_name[model_costs.QUEUE]._concurrency == 1
        assert queues_by_name["keystone-flows"]._concurrency == 4
        # A scan can be performed by either process; both remain individually healthy.
        time.sleep(6)
        assert status(client)["status"] == "ready"


def test_graceful_drain_keeps_other_worker_running(client, tmp_path, pair):
    code = f"""
import asyncio, os
from pathlib import Path
from keystone.platform.files.tasks import workflows as files
from keystone.entrypoints.worker import run
from keystone.core.asyncio import run as run_async
from keystone.core.config import Settings
root = Path({str(tmp_path)!r})
original = files.cleanup
async def held(*args, **kwargs):
    if os.environ['KEYSTONE_WORKER_ID'] == {FIRST!r}:
        (root / 'cleanup-active').touch()
        while not (root / 'release-cleanup').exists(): await asyncio.sleep(.05)
    return await original(*args, **kwargs)
files.cleanup = held
async def serve():
    stop = asyncio.Event()
    async def watch():
        while not (root / 'stop-a').exists(): await asyncio.sleep(.05)
        stop.set()
    watcher = asyncio.create_task(watch())
    try: await run(Settings(), stop)
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
run_async(serve())
"""
    with worker(client, tmp_path, code=code) as (a, la):
        wait_for(client, a, la, lambda: (tmp_path / "cleanup-active").exists())
        with worker(client, tmp_path, worker_id=SECOND) as (b, lb):
            wait_for(
                client,
                b,
                lb,
                lambda: any(
                    r["worker_id"] == SECOND and r["status"] == "ready"
                    for r in status(client)["instances"]
                ),
            )
            (tmp_path / "stop-a").touch()
            wait_for(
                client,
                a,
                la,
                lambda: any(
                    r["worker_id"] == FIRST and r["state"] == "draining"
                    for r in status(client)["instances"]
                ),
            )
            assert b.poll() is None
            (tmp_path / "release-cleanup").touch()
            a.wait(timeout=20)
            assert a.returncode == 0, la.read_text(encoding="utf8")
            assert (
                next(r for r in status(client)["instances"] if r["worker_id"] == FIRST)["state"]
                == "stopped"
            )
            identifier = start(client, upload(client).json()["id"]).json()["id"]
            assert wait_run(client, identifier, b, lb)["status"] == "succeeded"


def held_model_code(tmp_path):
    return f"""
import asyncio, json, os
from pathlib import Path
import httpx2
from keystone.platform.model_access.adapters.http import HttpClient
original = HttpClient.transport
root = Path({str(tmp_path)!r})
class Held(httpx2.AsyncByteStream):
    def __init__(self, content): self.content = content
    async def __aiter__(self):
        while not (root / ('release-' + os.environ['KEYSTONE_WORKER_ID'])).exists():
            await asyncio.sleep(0.05)
        yield self.content
def transport(self, provider):
    async def respond(request):
        response = await original(self, provider).handle_async_request(request)
        if request.method == 'POST':
            identifier = request.headers['x-keystone-invocation-id']
            with (root / 'send-counts').open('a') as counts:
                counts.write(identifier + '\\n')
            (root / ('sent-' + identifier)).write_text(os.environ['KEYSTONE_WORKER_ID'])
            return httpx2.Response(response.status_code,
                headers={{'content-type': 'application/json',
                    'x-request-id': 'mock-' + identifier}},
                stream=Held(response.content))
        return response
    return httpx2.MockTransport(respond)
HttpClient.transport = transport
from keystone.entrypoints.worker import main
main()
"""


def test_flow_global_limit_is_shared_by_both_executors(client, tmp_path, pair):
    pair.flow_global_concurrency = 1
    code = held_model_code(tmp_path)
    engine = client.app.state.runtime.flow_engine
    source = upload(client).json()["id"]
    with (
        worker(client, tmp_path, code=code) as (a, la),
        worker(client, tmp_path, code=code, worker_id=SECOND) as (b, lb),
    ):
        wait_for(
            client,
            a,
            la,
            lambda: status(client)["status"] == "ready" and status(client)["ready_workers"] == 2,
        )
        first = start(client, source, key="global-first").json()["id"]
        wait_for(client, a, la, lambda: list(tmp_path.glob("sent-*")))
        second = start(client, source, key="global-second").json()["id"]
        wait_for(client, b, lb, lambda: client.portal.call(engine.state, UUID(second)))
        # Both listeners are ready; the second execution cannot exceed the persisted limit.
        time.sleep(6)
        assert client.portal.call(engine.state, UUID(first))["status"] == "PENDING"
        assert client.portal.call(engine.state, UUID(second))["status"] == "ENQUEUED"
        assert len(list(tmp_path.glob("sent-*"))) == 1
        for identifier in (FIRST, SECOND):
            (tmp_path / ("release-" + identifier)).touch()
        assert wait_run(client, first, a, la)["status"] == "succeeded"
        assert wait_run(client, second, b, lb)["status"] == "succeeded"


def test_restart_only_closes_its_own_model_call_without_resending(client, tmp_path, pair):
    code = held_model_code(tmp_path)
    source = upload(client).json()["id"]

    def calls(run):
        return client.get(f"/api/v1/model-invocations?flow_run_id={run['id']}").json()

    with worker(client, tmp_path, code=code) as (a, la):
        first = start(client, source, key="owner-a").json()
        wait_for(client, a, la, lambda: calls(first) and calls(first)[0]["upstream_request_id"])
        with worker(client, tmp_path, code=code, worker_id=SECOND) as (b, lb):
            second = start(client, source, key="owner-b").json()
            wait_for(
                client, b, lb, lambda: calls(second) and calls(second)[0]["upstream_request_id"]
            )
            own_a, own_b = (
                stored(client, calls(first)[0]["id"]),
                stored(client, calls(second)[0]["id"]),
            )
            assert own_a.worker_id == FIRST and own_b.worker_id == SECOND
            kill(a)
            assert calls(second)[0]["status"] == "running"
            with worker(client, tmp_path, code=code) as (restarted, lr):
                wait_for(client, restarted, lr, lambda: calls(first)[0]["status"] == "unknown")
                assert calls(second)[0]["status"] == "running"
                assert stored(client, str(own_b.id)).worker_instance_id == own_b.worker_instance_id
                (tmp_path / ("release-" + SECOND)).touch()
                ended_b = wait_run(client, second["id"], b, lb)
                assert ended_b["status"] == "succeeded", (ended_b, lb.read_text(encoding="utf8"))
                ended_a = wait_run(client, first["id"], restarted, lr)
                assert (
                    ended_a["status"] == "failed"
                    and ended_a["error_code"] == "model_result_unavailable"
                )
                assert len([p for p in tmp_path.glob("sent-*") if p.read_text() == FIRST]) == 1
                wait_for(
                    client,
                    restarted,
                    lr,
                    lambda: view(client, own_a.id)["cost_check_status"] == "confirmed",
                )
                assert view(client, own_a.id)["status"] == "unknown"


def billing_code(root, *, crash=False, hold=False):
    return f"""
import asyncio, hashlib, json, os
from pathlib import Path
import httpx2
from keystone.platform.model_access.adapters.http import HttpClient
from keystone.platform.model_access.services.costs import CostService
root = Path({str(root)!r})
original = HttpClient.transport
async def respond(request):
    if request.url.path == '/api/status':
        return httpx2.Response(200, json={{'success':True,'data':{{
            'quota_per_unit':500000,'currency_type':'USD'}}}})
    assert request.url.path == '/api/log/token'
    page, size = int(request.url.params['p']), int(request.url.params['page_size'])
    (root / ('read-' + os.environ['KEYSTONE_WORKER_ID'] + '-' + str(page))).touch()
    if {hold!r}:
        scope = hashlib.sha256(request.url.params['key'].encode()).hexdigest()[:16]
        marker = root / ('active-' + scope)
        marker.touch()
        try:
            while not (root / 'release').exists(): await asyncio.sleep(0.05)
        finally: marker.unlink(missing_ok=True)
    rows=json.loads((root/'records.json').read_text())
    return httpx2.Response(200,json={{'success':True,'data':{{
        'items':rows[(page-1)*size:page*size],
        'total':len(rows),'page':page,'page_size':size}}}})
HttpClient.transport=lambda self,p: (
    httpx2.MockTransport(respond) if p=='openlux' else original(self,p))
if {crash!r}:
    original_commit=CostService._commit
    async def commit(self, config, checkpoint, lease, **kwargs):
        if checkpoint['page'] == 4:
            try:
                with (root/'crashed').open('x') as f: f.write(os.environ['KEYSTONE_WORKER_ID'])
            except FileExistsError: pass
            else: os._exit(91)
        return await original_commit(self, config, checkpoint, lease, **kwargs)
    CostService._commit=commit
from keystone.entrypoints.worker import main
main()
"""


def test_billing_page_crash_resumes_by_original_identity_with_other_worker_alive(
    client, tmp_path, pair, billing
):
    configure(client, key="relay")
    identifier = call(client, key="relay")
    due(client, identifier)
    records = [receipt(f"unrelated-{i}") for i in range(40)] + billing["records"]
    (tmp_path / "records.json").write_text(json.dumps(records), encoding="utf8")
    code = billing_code(tmp_path, crash=True)
    with (
        worker(client, tmp_path, code=code) as (a, la),
        worker(client, tmp_path, code=code, worker_id=SECOND) as (b, lb),
    ):
        deadline = time.monotonic() + 40
        while not (tmp_path / "crashed").exists():
            assert time.monotonic() < deadline, (
                la.read_text(encoding="utf8"),
                lb.read_text(encoding="utf8"),
            )
            time.sleep(0.1)
        failed = (tmp_path / "crashed").read_text()
        dead, live, live_log = (a, b, lb) if failed == FIRST else (b, a, la)
        dead.wait(timeout=15)
        assert dead.returncode != 0 and live.poll() is None
        assert view(client, identifier)["cost_amount"] is None
        with worker(client, tmp_path, code=code, worker_id=failed) as (restarted, lr):
            wait_for(
                client,
                restarted,
                lr,
                lambda: view(client, identifier)["cost_check_status"] == "confirmed",
            )
            assert live.poll() is None, live_log.read_text(encoding="utf8")
            assert view(client, identifier)["cost_amount"] == "0.002656000000000000"
            assert len([c for c in billing["calls"] if c[0] == "POST"]) == 1


def test_manual_cost_shares_worker_capacity_and_busy_does_not_spend_retry(
    client, tmp_path, pair, billing
):
    identifiers = []
    for index in range(3):
        key = f"relay{index}"
        configure(client, key=key, api_key=f"different-token-{index}")
        identifiers.append(call(client, key=key))
    # Only two due scopes are scheduled; the third is reserved for a manual request.
    for identifier in identifiers[:2]:
        due(client, identifier)
    (tmp_path / "records.json").write_text(json.dumps(billing["records"]), encoding="utf8")
    code = billing_code(tmp_path, hold=True)
    with (
        worker(client, tmp_path, code=code) as (a, la),
        worker(client, tmp_path, code=code, worker_id=SECOND) as (b, lb),
    ):
        wait_for(client, a, la, lambda: len(list(tmp_path.glob("active-*"))) == 2)
        response = client.post(f"/api/v1/model-invocations/{identifiers[2]}/cost")
        assert response.status_code == 409 and response.json()["code"] == "model_capacity_busy"
        assert view(client, identifiers[2])["cost_check_errors"] == 0
        assert len(list(tmp_path.glob("active-*"))) == 2
        (tmp_path / "release").touch()
        for identifier in identifiers[:2]:
            wait_for(
                client,
                a,
                la,
                lambda identifier=identifier: (
                    view(client, identifier)["cost_check_status"] == "confirmed"
                ),
            )


def test_lost_scope_connection_cannot_write_old_fee(client, billing, monkeypatch):
    configure(client, key="relay")
    identifier = call(client, key="relay")
    due(client, identifier)
    coordination = client.app.state.runtime.models.coordination
    original = coordination.cost_scope
    leases = []

    @asynccontextmanager
    async def captured(scope):
        async with original(scope) as lease:
            leases.append(lease)
            yield lease

    monkeypatch.setattr(coordination, "cost_scope", captured)

    async def invalidate():
        await leases[-1].connection.invalidate()

    billing["on_log"] = invalidate
    with pytest.raises(ServiceError) as error:
        process(client, identifier)
    assert error.value.code == "model_refresh_interrupted"
    assert view(client, identifier)["cost_amount"] is None
    assert view(client, identifier)["cost_check_errors"] == 0
    billing["on_log"] = None
    process(client, identifier)
    assert view(client, identifier)["cost_check_status"] == "confirmed"


def test_conflicting_worker_does_not_change_live_plans_or_heartbeat(client, tmp_path, pair):
    code = f"""
import os, time
from pathlib import Path
from unittest.mock import patch
from keystone.entrypoints import worker as entrypoint
from keystone.core.local_leases import LocalLeases
root = Path({str(tmp_path)!r})
overrides = (
    patch.dict(os.environ, {{'KEYSTONE_MODEL_COST_CONCURRENCY': '3'}}),
    patch('socket.gethostname', return_value='unsupported-other-host'),
    patch.object(entrypoint, 'execution_version', return_value='other-release'),
    patch.object(LocalLeases, 'fingerprint', return_value='0' * 64),
)
for index, override in enumerate(overrides):
    with override:
        try:
            entrypoint.main()
        except SystemExit as error:
            assert error.code == 1, error.code
        else:
            raise AssertionError('Conflicting Worker started')
    print('conflict_rejected', index, flush=True)
    (root / f'rejected-{{index}}').touch()
    deadline = time.monotonic() + 15
    while not (root / f'checked-{{index}}').exists():
        assert time.monotonic() < deadline, 'Parent did not check the rejection'
        time.sleep(.05)
"""
    with worker(client, tmp_path) as (a, la):
        wait_for(client, a, la, lambda: status(client)["ready_workers"] == 1)
        plans = schedules(client)
        # Reuse only the rejected interpreter; each main() constructs and closes its runtime.
        with worker(client, tmp_path, code=code, worker_id=SECOND) as (b, lb):
            offset = 0
            for index, override in enumerate(("concurrency", "host", "version", "coordination")):
                wait_for(
                    client,
                    b,
                    lb,
                    lambda index=index: (tmp_path / f"rejected-{index}").exists(),
                    timeout=15,
                )
                output = lb.read_text(encoding="utf8")
                assert "worker_deployment_conflict" in output[offset:], override
                offset = len(output)
                current = schedules(client)
                assert {k: (v["schedule_id"], v["status"]) for k, v in current.items()} == {
                    k: (v["schedule_id"], v["status"]) for k, v in plans.items()
                }, override
                assert a.poll() is None, la.read_text(encoding="utf8")
                assert status(client)["ready_workers"] == 1, override
                assert all(r["worker_id"] != SECOND for r in status(client)["instances"]), override
                (tmp_path / f"checked-{index}").touch()
            assert b.wait(timeout=15) == 0, lb.read_text(encoding="utf8")


def test_inference_budget_shared_by_independent_clients_and_token_aliases(client):
    runtime = client.app.state.runtime
    runtime.settings.model_inference_concurrency = 2
    runtime.settings.model_scope_concurrency = 1
    other = QueryCoordination(runtime.database, runtime.settings)

    async def capacity():
        async with runtime.models.coordination.inference("token-a"):
            with pytest.raises(ServiceError) as scope_busy:
                async with other.inference("token-a"):
                    pytest.fail("same token got two permits")
            assert scope_busy.value.code == "model_capacity_busy"
            async with other.inference("token-b"):
                with pytest.raises(ServiceError) as globally_busy:
                    async with QueryCoordination(runtime.database, runtime.settings).inference(
                        "token-c"
                    ):
                        pytest.fail("global limit was bypassed")
                assert globally_busy.value.code == "model_capacity_busy"
        async with other.inference("token-a"):
            pass

    client.portal.call(capacity)


def test_stale_page_revision_is_rejected_without_changing_fee(client, billing):
    configure(client, key="relay")
    identifier = call(client, key="relay")
    due(client, identifier)

    async def advance():
        # An administrative/recovery change races the in-memory response.
        async with client.app.state.runtime.database.transaction() as session:
            owner = await session.scalar(select(Channel).where(Channel.provider == "openlux"))
            owner.cost_checkpoint = {**owner.cost_checkpoint, "revision": 99}

    billing["on_log"] = advance
    with pytest.raises(ServiceError) as error:
        process(client, identifier)
    assert error.value.code == "model_configuration_changed"
    assert view(client, identifier)["cost_amount"] is None
    owner = query(
        client, lambda session: session.scalar(select(Channel).where(Channel.provider == "openlux"))
    )
    assert owner.cost_checkpoint["revision"] == 99 and owner.cost_checkpoint["page"] == 1

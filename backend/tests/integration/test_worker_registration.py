"""Hot addition, durable membership and retirement under real identity locks."""

import asyncio
import time
from contextlib import asynccontextmanager
from functools import partial
from threading import Event
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from support.flow_engine import flow_engine as flow_engine
from test_files_dictionary import client as client
from test_files_dictionary import upload
from test_flow import configuration as configuration
from test_flow import start, wait_run, worker
from test_identity import query
from test_maintenance import schedules, wait_for
from test_model_access import invoke
from test_multi_workers import FIRST, SECOND, held_model_code, status

from keystone.composition import execution_version
from keystone.core.errors import ServiceError
from keystone.core.models import WorkerHeartbeat, WorkerRegistration
from keystone.core.worker_health import WorkerHealth
from keystone.core.worker_identity import config_digest
from keystone.core.worker_registration import identity_lease
from keystone.platform.audit.services import writer as audit
from keystone.platform.model_access.models import ModelInvocation
from keystone.worker import lifecycle

pytestmark = [pytest.mark.postgres, pytest.mark.usefixtures("flow_engine")]


def seed(client, worker_id=SECOND):
    query(
        client,
        lambda s: s.merge(
            WorkerRegistration(
                worker_id=worker_id, state="registered", stopped_at=func.clock_timestamp()
            )
        ),
    )


def change(client, worker_id=SECOND, **kwargs):
    return client.portal.call(
        partial(lifecycle.change_worker, client.app.state.runtime.settings, worker_id, **kwargs)
    )


def member(client, worker_id=SECOND):
    return query(client, lambda s: s.get(WorkerRegistration, worker_id))


def stop_code(tmp_path):
    return f"""
import asyncio, os
from pathlib import Path
from keystone.entrypoints.worker import run
from keystone.core.asyncio import run as run_async
from keystone.core.config import Settings
async def serve():
    stop = asyncio.Event()
    async def watch():
        path = Path({str(tmp_path)!r}) / ('stop-' + os.environ['KEYSTONE_WORKER_ID'])
        while not path.exists(): await asyncio.sleep(.05)
        stop.set()
    watcher = asyncio.create_task(watch())
    try: await run(Settings(), stop)
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
run_async(serve())
"""


def test_hot_add_without_api_or_existing_worker_restart(client, tmp_path):
    runtime = client.app.state.runtime
    runtime.settings.flow_worker_concurrency = 1
    source = upload(client).json()["id"]
    code = held_model_code(tmp_path)
    with worker(client, tmp_path, code=code) as (a, la):
        first = start(client, source, key="hot-a").json()["id"]
        wait_for(client, a, la, lambda: list(tmp_path.glob("sent-*")))
        identity = member(client, FIRST).instance_id
        plans = schedules(client)
        with worker(client, tmp_path, code=code, worker_id=SECOND) as (b, lb):
            wait_for(client, b, lb, lambda: status(client)["ready_workers"] == 2)
            assert status(client)["expected_workers"] == 2
            assert client.app.state.runtime is runtime
            assert member(client, FIRST).instance_id == identity and a.poll() is None
            assert (
                client.get(f"/api/v1/model-invocations?flow_run_id={first}").json()[0]["status"]
                == "running"
            )
            second = start(client, source, key="hot-b").json()["id"]
            wait_for(client, b, lb, lambda: len(list(tmp_path.glob("sent-*"))) == 2)
            assert {
                r.worker_id
                for r in query(client, lambda s: s.scalars(select(WorkerRegistration))).all()
            } == {FIRST, SECOND}
            assert {k: v["schedule_id"] for k, v in schedules(client).items()} == {
                k: v["schedule_id"] for k, v in plans.items()
            }
            for name in (FIRST, SECOND):
                (tmp_path / ("release-" + name)).touch()
            assert wait_run(client, first, a, la)["status"] == "succeeded"
            assert wait_run(client, second, b, lb)["status"] == "succeeded"
            sent = (tmp_path / "send-counts").read_text().splitlines()
            rows = client.get("/api/v1/model-invocations").json()
            assert len(sent) == len(set(sent)) == len(rows)
            assert set(sent) == {row["id"] for row in rows}


def test_retire_only_stopped_target_while_api_and_other_worker_remain_live(client, tmp_path):
    code = stop_code(tmp_path)
    with (
        worker(client, tmp_path, code=code) as (a, la),
        worker(client, tmp_path, code=code, worker_id=SECOND) as (b, lb),
    ):
        wait_for(client, a, la, lambda: status(client)["ready_workers"] == 2)
        with pytest.raises(ServiceError):
            change(client)
        assert member(client).state == "registered"
        first_instance = member(client, FIRST).instance_id
        (tmp_path / ("stop-" + SECOND)).touch()
        assert b.wait(timeout=20) == 0, lb.read_text(encoding="utf8")
        assert status(client)["expected_workers"] == 2
        assert status(client)["status"] == "unready"
        change(client)
        assert member(client).state == "retired"
        assert member(client, FIRST).instance_id == first_instance
        assert a.poll() is None and client.get("/api/v1/ready").status_code == 200
        assert status(client)["expected_workers"] == status(client)["ready_workers"] == 1
        with worker(client, tmp_path, worker_id=SECOND) as (rejected, log):
            assert rejected.wait(timeout=12) == 1
            assert "worker_retired" in log.read_text(encoding="utf8")
        change(client, reactivate=True)
        with worker(client, tmp_path, worker_id=SECOND) as (restored, lr):
            wait_for(client, restored, lr, lambda: status(client)["ready_workers"] == 2)
            assert member(client).instance_id is not None and a.poll() is None
        assert len(client.get("/api/v1/audit-logs?action=worker.retire").json()) == 1
        assert len(client.get("/api/v1/audit-logs?action=worker.reactivate").json()) == 1


def test_retirement_holds_target_identity_against_concurrent_real_start(
    client, tmp_path, monkeypatch
):
    seed(client)
    entered, release = Event(), Event()

    async def held(*args):
        entered.set()
        while not release.is_set():
            await asyncio.sleep(0.01)
        return False

    monkeypatch.setattr(lifecycle, "has_owned_work", held)
    future = client.portal.start_task_soon(
        lifecycle.change_worker, client.app.state.runtime.settings, SECOND
    )
    try:
        assert entered.wait(3)
        code = """
from keystone.entrypoints import worker as entry
original = entry.run_executor
async def observed(*args, **kwargs):
    try: return await original(*args, **kwargs)
    except Exception as error:
        print('identity_rejected:' + getattr(error, 'code', type(error).__name__), flush=True)
        raise
entry.run_executor = observed
entry.main()
"""
        with worker(client, tmp_path, worker_id=SECOND, code=code) as (process, log):
            deadline = time.monotonic() + 7
            while "identity_rejected:file_busy" not in log.read_text(encoding="utf8"):
                assert time.monotonic() < deadline, log.read_text(encoding="utf8")
                time.sleep(0.02)
            assert member(client).state == "registered"
            release.set()
            assert process.wait(timeout=12) == 1
    finally:
        release.set()
        future.result(timeout=5)
    assert member(client).state == "retired"
    assert status(client)["expected_workers"] == 0 and status(client)["status"] == "offline"


def test_admission_rolls_back_when_identity_connection_is_lost(client, monkeypatch):
    from keystone.core import worker_health

    runtime = client.app.state.runtime
    original = worker_health.admit

    async def exercise():
        health = WorkerHealth(
            runtime.database,
            runtime.settings.model_copy(update={"worker_id": SECOND}),
            execution_version(),
        )
        async with identity_lease(runtime.database, runtime.storage, SECOND) as lease:

            async def lost(session, *args, **kwargs):
                row = await original(session, *args, **kwargs)
                await session.flush()
                async with runtime.database.transaction() as other:
                    assert await other.scalar(
                        text("SELECT pg_terminate_backend(:pid)"), {"pid": lease.pid}
                    )
                return row

            monkeypatch.setattr(worker_health, "admit", lost)
            with pytest.raises((DBAPIError, ServiceError)):
                await health.claim(runtime.storage.coordination.fingerprint(), lease)
            assert not health.claimed

    client.portal.call(exercise)
    assert member(client) is None
    assert not query(client, lambda s: s.scalars(select(WorkerHeartbeat))).all()


def test_final_stop_and_same_identity_restart_use_consistent_row_lock_order(client):
    runtime = client.app.state.runtime

    async def exercise():
        async def admitted():
            health = WorkerHealth(runtime.database, runtime.settings, execution_version())
            async with identity_lease(runtime.database, runtime.storage, FIRST) as lease:
                await health.claim(runtime.storage.coordination.fingerprint(), lease)
            return health

        old = await admitted()
        async with asyncio.timeout(8):
            for _ in range(5):
                fresh, _ = await asyncio.gather(admitted(), old.publish("stopped"))
                async with runtime.database.transaction() as s:
                    row = await s.get(WorkerRegistration, FIRST)
                    assert row.instance_id == fresh.instance_id and row.stopped_at is None
                old = fresh

    client.portal.call(exercise)


@pytest.mark.parametrize("state", ["PENDING", "ENQUEUED", "DELAYED"])
def test_bound_unfinished_execution_blocks_retirement_but_shared_queue_does_not(client, state):
    seed(client)
    runtime = client.app.state.runtime

    async def pending():
        await runtime.flow_engine.client.enqueue_async(
            {
                "workflow_name": "unregistered-test",
                "workflow_id": "owned-test",
                "queue_name": "keystone-system",
            }
        )
        async with runtime.database.transaction() as s:
            await s.execute(
                text(
                    "UPDATE dbos.workflow_status SET executor_id=:id,status=:state "
                    "WHERE workflow_uuid='owned-test'"
                ),
                {"id": SECOND, "state": state},
            )

    client.portal.call(pending)
    with pytest.raises(ServiceError) as blocked:
        change(client)
    assert blocked.value.code == "worker_recovery_required"
    assert member(client).state == "registered"

    async def unbind(s):
        await s.execute(
            text(
                "UPDATE dbos.workflow_status SET executor_id=NULL WHERE workflow_uuid='owned-test'"
            )
        )

    query(client, unbind)
    change(client)
    assert member(client).state == "retired"


def test_running_model_call_blocks_retirement_without_altering_fee_or_result(client):
    seed(client)
    identifier = UUID(invoke(client).json()["invocation_id"])

    async def interrupted(s):
        row = await s.get(ModelInvocation, identifier)
        row.status, row.finished_at = "running", None
        row.worker_id, row.worker_instance_id, row.execution_version = (
            SECOND,
            uuid4(),
            execution_version(),
        )

    query(client, interrupted)
    with pytest.raises(ServiceError) as blocked:
        change(client)
    assert blocked.value.code == "worker_recovery_required"
    assert query(client, lambda s: s.get(ModelInvocation, identifier)).status == "running"
    assert member(client).state == "registered"


def test_last_registration_slot_is_atomic_and_offline_members_keep_their_slots(client):
    for name in (FIRST, SECOND, "keystone-worker-03"):
        seed(client, name)
    runtime = client.app.state.runtime

    async def join(name):
        health = WorkerHealth(
            runtime.database,
            runtime.settings.model_copy(update={"worker_id": name}),
            execution_version(),
        )
        async with identity_lease(runtime.database, runtime.storage, name) as lease:
            await health.claim(runtime.storage.coordination.fingerprint(), lease)
        return name

    async def race():
        return await asyncio.gather(
            join("keystone-worker-04"), join("keystone-worker-05"), return_exceptions=True
        )

    results = client.portal.call(race)
    assert sum(isinstance(r, str) for r in results) == 1
    failures = [r for r in results if isinstance(r, ServiceError)]
    assert len(failures) == 1 and failures[0].code == "worker_capacity_full"
    assert status(client)["expected_workers"] == 4
    assert (
        query(client, lambda s: s.scalar(select(func.count()).select_from(WorkerRegistration))) == 4
    )


@pytest.mark.parametrize("reactivate", [False, True])
def test_lifecycle_rolls_back_if_its_identity_connection_is_lost(client, monkeypatch, reactivate):
    seed(client)
    if reactivate:
        change(client)
    original_state = member(client).state
    runtime = client.app.state.runtime
    original_lease, original_record = lifecycle.identity_lease, audit.record
    leases = []

    @asynccontextmanager
    async def captured(*args):
        async with original_lease(*args) as lease:
            leases.append(lease)
            yield lease

    async def interrupted(s, action, *args, **kwargs):
        await original_record(s, action, *args, **kwargs)
        if action == ("worker.reactivate" if reactivate else "worker.retire"):
            async with runtime.database.transaction() as other:
                assert await other.scalar(
                    text("SELECT pg_terminate_backend(:pid)"), {"pid": leases[-1].pid}
                )

    monkeypatch.setattr(lifecycle, "identity_lease", captured)
    monkeypatch.setattr(audit, "record", interrupted)
    with pytest.raises((DBAPIError, ServiceError)):
        change(client, reactivate=reactivate)
    assert member(client).state == original_state
    action = "worker.reactivate" if reactivate else "worker.retire"
    assert not client.get(f"/api/v1/audit-logs?action={action}").json()


def test_reactivation_checks_capacity_and_is_idempotent(client):
    for name in (FIRST, SECOND, "keystone-worker-03", "keystone-worker-04"):
        seed(client, name)
    change(client)
    seed(client, "keystone-worker-05")
    with pytest.raises(ServiceError) as blocked:
        change(client, reactivate=True)
    assert blocked.value.code == "worker_capacity_full"
    assert member(client).state == "retired"
    assert not client.get("/api/v1/audit-logs?action=worker.reactivate").json()
    change(client, "keystone-worker-05")
    change(client, reactivate=True)
    change(client, reactivate=True)
    assert member(client).state == "registered" and member(client).instance_id is None
    assert status(client)["expected_workers"] == 4
    assert len(client.get("/api/v1/audit-logs?action=worker.reactivate").json()) == 1


def test_failed_execution_inspection_never_allows_retirement(client, monkeypatch):
    from dbos import DBOSClient

    seed(client)

    async def broken(*args, **kwargs):
        raise RuntimeError("credential-must-not-leak")

    monkeypatch.setattr(DBOSClient, "list_workflows_async", broken)
    with pytest.raises(ServiceError) as failed:
        change(client)
    assert failed.value.code == "worker_inspection_unavailable"
    assert "credential" not in str(failed.value)
    assert member(client).state == "registered"


def test_old_heartbeat_cannot_restore_retired_membership_and_history_cleanup_keeps_identity(client):
    seed(client)
    runtime = client.app.state.runtime
    health = WorkerHealth(
        runtime.database,
        runtime.settings.model_copy(update={"worker_id": SECOND}),
        execution_version(),
    )
    health.claimed = True

    async def registered(s):
        row = await s.get(WorkerRegistration, SECOND)
        row.instance_id = health.instance_id
        row.execution_version, row.config_digest = (
            execution_version(),
            config_digest(runtime.settings),
        )

    query(client, registered)
    client.portal.call(health.publish, "stopped")
    change(client)
    client.portal.call(health.publish, "ready")
    assert member(client).state == "retired"
    assert status(client)["expected_workers"] == 0
    assert status(client)["status"] == "offline"

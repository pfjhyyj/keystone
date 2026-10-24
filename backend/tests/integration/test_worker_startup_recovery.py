"""Worker identity ownership, startup ordering and resumable recovery batches."""

import asyncio
from contextlib import asynccontextmanager
from threading import Event
from uuid import UUID, uuid4

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.exc import DBAPIError
from test_files_dictionary import client as client
from test_flow import configuration as configuration
from test_flow import worker as worker_process
from test_identity import query
from test_maintenance import wait_for
from test_model_access import invoke

from keystone.composition import execution_version
from keystone.core.errors import ServiceError
from keystone.core.execution import runtime as dbos_runtime
from keystone.core.worker_health import WorkerHealth
from keystone.core.worker_registration import identity_lease
from keystone.entrypoints import worker
from keystone.platform.audit.models import AuditLog
from keystone.platform.audit.services import writer as audit
from keystone.platform.model_access.models import ModelInvocation
from keystone.platform.model_access.services.recovery import recover_interrupted_calls
from keystone.worker.lifecycle import change_worker

pytestmark = pytest.mark.postgres


def capture_identity(monkeypatch):
    original = worker.identity_lease
    leases = []

    @asynccontextmanager
    async def captured(*args, **kwargs):
        async with original(*args, **kwargs) as lease:
            leases.append(lease)
            yield lease

    monkeypatch.setattr(worker, "identity_lease", captured)
    monkeypatch.setattr(worker, "register_workflows", lambda: None)
    return leases


def test_original_identity_recovery_rolls_back_after_identity_connection_loss(client, monkeypatch):
    runtime = client.app.state.runtime
    identifier = UUID(invoke(client).json()["invocation_id"])

    async def interrupted(session):
        row = await session.get(ModelInvocation, identifier)
        row.status, row.finished_at = "running", None
        row.flow_context = {"flow_run_id": str(uuid4())}
        row.worker_id, row.worker_instance_id, row.execution_version = (
            runtime.settings.worker_id,
            uuid4(),
            execution_version(),
        )

    query(client, interrupted)
    leases = capture_identity(monkeypatch)
    original_record = audit.record
    launched = []

    async def disconnected(session, action, *args, **kwargs):
        await original_record(session, action, *args, **kwargs)
        if action == "model.invocation.recover":
            async with runtime.database.transaction() as other:
                assert await other.scalar(
                    text("SELECT pg_terminate_backend(:pid)"), {"pid": leases[-1].pid}
                )

    async def exercise():
        stop, ready = asyncio.Event(), asyncio.Event()

        @asynccontextmanager
        async def executor(*args, **kwargs):
            launched.append(True)
            stop.set()
            yield

        monkeypatch.setattr(worker, "register_maintenance", lambda *args: ({}, []))
        monkeypatch.setattr(worker, "dbos_lifespan", executor)
        health = WorkerHealth(runtime.database, runtime.settings, execution_version())
        with pytest.raises((DBAPIError, ServiceError)):
            await worker.run_executor(runtime, stop, ready=ready, health=health)
        assert not ready.is_set()

    monkeypatch.setattr(audit, "record", disconnected)
    client.portal.call(exercise)
    assert not launched
    assert query(client, lambda s: s.get(ModelInvocation, identifier)).status == "running"
    assert not client.get("/api/v1/audit-logs?action=model.invocation.recover").json()


def test_identity_loss_during_queue_setup_cannot_launch_executor(client, monkeypatch):
    runtime = client.app.state.runtime
    leases = capture_identity(monkeypatch)
    original_register = dbos_runtime.DBOSClient.register_queue_async
    launched = []
    stop = None
    terminated = False

    async def disconnected(*args, **kwargs):
        nonlocal terminated
        await original_register(*args, **kwargs)
        if terminated:
            return
        terminated = True
        async with runtime.database.transaction() as other:
            assert await other.scalar(
                text("SELECT pg_terminate_backend(:pid)"), {"pid": leases[-1].pid}
            )

    class Executor:
        workflow_id = None

        def __init__(self, **kwargs):
            pass

        @staticmethod
        def listen_queues(*args):
            pass

        @staticmethod
        def launch():
            launched.append(True)
            stop.set()

        @staticmethod
        def destroy(**kwargs):
            pass

    monkeypatch.setattr(dbos_runtime.DBOSClient, "register_queue_async", disconnected)
    monkeypatch.setattr(dbos_runtime, "DBOS", Executor)
    monkeypatch.setattr(
        worker, "register_maintenance", lambda *args: ({"keystone-system": (2, 2)}, [])
    )

    async def exercise():
        nonlocal stop
        stop, ready = asyncio.Event(), asyncio.Event()
        health = WorkerHealth(runtime.database, runtime.settings, execution_version())
        with pytest.raises((DBAPIError, ServiceError)):
            await worker.run_executor(runtime, stop, ready=ready, health=health)
        assert not ready.is_set()

    client.portal.call(exercise)
    assert not launched


def test_identity_loss_while_installing_schedules_cannot_report_ready(client, monkeypatch):
    runtime = client.app.state.runtime
    leases = capture_identity(monkeypatch)

    async def disconnected():
        assert runtime.worker_lease is leases[-1]
        async with runtime.database.transaction() as other:
            assert await other.scalar(
                text("SELECT pg_terminate_backend(:pid)"), {"pid": leases[-1].pid}
            )

    @asynccontextmanager
    async def executor(*args, **kwargs):
        yield

    monkeypatch.setattr(worker, "dbos_lifespan", executor)
    monkeypatch.setattr(worker, "register_maintenance", lambda *args: ({}, [disconnected]))

    async def exercise():
        ready = asyncio.Event()
        health = WorkerHealth(runtime.database, runtime.settings, execution_version())
        with pytest.raises((DBAPIError, ServiceError)):
            await worker.run_executor(runtime, asyncio.Event(), ready=ready, health=health)
        assert not ready.is_set() and runtime.worker_lease is None

    client.portal.call(exercise)


def test_identity_checks_do_not_overlap_another_transaction_on_same_connection(client):
    runtime = client.app.state.runtime

    async def exercise():
        async with identity_lease(
            runtime.database, runtime.storage, runtime.settings.worker_id
        ) as lease:
            entered, release = asyncio.Event(), asyncio.Event()

            async def transaction():
                async with lease.transaction() as session:
                    assert await session.scalar(text("SELECT pg_backend_pid()")) == lease.pid
                    entered.set()
                    await release.wait()

            held = asyncio.create_task(transaction())
            checked = None
            try:
                await entered.wait()
                checked = asyncio.create_task(lease())
                await asyncio.sleep(0.05)
                assert not checked.done()
            finally:
                release.set()
                await held
                if checked is not None:
                    await checked

    client.portal.call(exercise)


def test_recovery_preserves_committed_batches_and_original_identity_can_finish(client, monkeypatch):
    runtime = client.app.state.runtime
    identifier = UUID(invoke(client).json()["invocation_id"])
    original = query(client, lambda s: s.get(ModelInvocation, identifier))
    previous = uuid4()

    async def interrupted(session):
        values = {c.key: getattr(original, c.key) for c in ModelInvocation.__table__.columns}
        values.update(
            status="running",
            finished_at=None,
            flow_context={"flow_run_id": str(uuid4())},
            worker_id=runtime.settings.worker_id,
            worker_instance_id=previous,
            execution_version=execution_version(),
        )
        await session.execute(
            insert(ModelInvocation), [{**values, "id": uuid4()} for _ in range(101)]
        )

    query(client, interrupted)
    recorded = 0
    original_record = audit.record

    async def recover(*, disconnect=False):
        nonlocal recorded
        health = WorkerHealth(runtime.database, runtime.settings, execution_version())
        async with identity_lease(runtime.database, runtime.storage, health.worker_id) as lease:
            await health.claim(runtime.storage.coordination.fingerprint(), lease)

            async def disconnected(session, action, *args, **kwargs):
                nonlocal recorded
                await original_record(session, action, *args, **kwargs)
                recorded += 1
                if recorded == 101:
                    async with runtime.database.transaction() as other:
                        assert await other.scalar(
                            text("SELECT pg_terminate_backend(:pid)"), {"pid": lease.pid}
                        )

            monkeypatch.setattr(audit, "record", disconnected if disconnect else original_record)
            return await recover_interrupted_calls(runtime.database, health.identity, lease=lease)

    async def interrupted_recovery():
        try:
            await recover(disconnect=True)
        except (DBAPIError, ServiceError):
            return
        raise AssertionError("Lost identity committed its recovery batch")

    client.portal.call(interrupted_recovery)
    rows = query(
        client,
        lambda s: s.scalars(
            select(ModelInvocation).where(ModelInvocation.worker_instance_id == previous)
        ),
    ).all()
    assert len(rows) == 101
    assert sum(row.status == "unknown" for row in rows) == 100
    assert sum(row.status == "running" for row in rows) == 1
    assert all(row.cost_amount == original.cost_amount for row in rows)

    def audits():
        return query(
            client,
            lambda s: s.scalars(
                select(AuditLog).where(AuditLog.action == "model.invocation.recover")
            ),
        ).all()

    assert len(audits()) == 100
    assert client.portal.call(recover) == 1
    assert client.portal.call(recover) == 0
    assert len(audits()) == 101


def test_blocked_startup_recovery_times_out_and_original_identity_can_retry(client, tmp_path):
    runtime = client.app.state.runtime
    identifier = UUID(invoke(client).json()["invocation_id"])

    async def interrupted(session):
        row = await session.get(ModelInvocation, identifier)
        row.status, row.finished_at = "running", None
        row.flow_context = {"flow_run_id": str(uuid4())}
        row.worker_id, row.worker_instance_id, row.execution_version = (
            runtime.settings.worker_id,
            uuid4(),
            execution_version(),
        )

    query(client, interrupted)
    held, release = Event(), Event()

    async def block():
        async with runtime.database.transaction() as session:
            await session.get(ModelInvocation, identifier, with_for_update=True)
            held.set()
            while not release.is_set():
                await asyncio.sleep(0.01)

    blocker = client.portal.start_task_soon(block)
    try:
        assert held.wait(3)
        with worker_process(client, tmp_path) as (process, log):
            assert process.wait(timeout=15) == 1, log.read_text(encoding="utf8")
            assert "TimeoutError" in log.read_text(encoding="utf8")
        assert query(client, lambda s: s.get(ModelInvocation, identifier)).status == "running"
        assert not client.get("/api/v1/audit-logs?action=model.invocation.recover").json()
        with pytest.raises(ServiceError) as blocked:
            client.portal.call(change_worker, runtime.settings, runtime.settings.worker_id)
        assert blocked.value.code == "worker_recovery_required"
    finally:
        release.set()
        blocker.result(timeout=5)
    with worker_process(client, tmp_path) as (process, log):
        wait_for(
            client,
            process,
            log,
            lambda: client.get("/api/v1/worker/status").json()["status"] == "ready",
        )
        assert query(client, lambda s: s.get(ModelInvocation, identifier)).status == "unknown"
        assert len(client.get("/api/v1/audit-logs?action=model.invocation.recover").json()) == 1

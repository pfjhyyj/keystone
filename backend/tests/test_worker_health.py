import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from keystone.core import worker_health
from keystone.core.config import Settings
from keystone.core.worker_health import WorkerHealth
from keystone.entrypoints import worker


def test_liveness_and_readiness_are_separate_and_old_checks_expire(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(worker_health.time, "monotonic", lambda: now[0])
    health = WorkerHealth(None)
    assert health.observation() == "starting"
    health.checked(True)
    assert health.observation() == "ready"
    assert health.observation(False) == "unready"
    now[0] += 31
    assert health.observation() == "unready"
    health.checked(False)
    assert health.observation() == "unready"


def test_process_readiness_does_not_require_personally_executing_shared_tasks(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(worker_health.time, "monotonic", lambda: now[0])
    health = WorkerHealth(None)
    health.require_task("files")
    health.require_task("flow")
    health.checked(True)
    assert health.observation() == "ready"
    health.checked_task("files", True)
    health.checked_task("flow", True)
    assert health.observation() == "ready"
    now[0] += 31
    health.checked(True)
    health.checked_task("flow", True)
    assert health.observation() == "ready"
    health.checked_task("files", False)
    assert health.observation() == "ready"
    health.checked_task("files", True)
    assert health.observation() == "ready"


@pytest.mark.parametrize("status", ["ready", "unready", "offline", "unknown"])
def test_probe_queries_database_without_starting_executor(monkeypatch, capsys, status):
    database = SimpleNamespace(close=AsyncMock())
    query = AsyncMock(return_value={"status": status})
    monkeypatch.setattr(worker, "Database", lambda *args, **kwargs: database)
    monkeypatch.setattr(worker, "worker_status", query)
    monkeypatch.setattr(worker, "Settings", lambda: Settings(config_file=None))
    monkeypatch.setattr(worker, "build_runtime", lambda _: pytest.fail("Probe started executor"))
    monkeypatch.setattr("sys.argv", ["keystone-worker", "--check"])
    with pytest.raises(SystemExit) as result:
        worker.main()
    assert result.value.code == (0 if status == "ready" else 1)
    assert capsys.readouterr().out.strip() == (
        "Worker healthy" if status == "ready" else "Worker unavailable"
    )
    assert query.call_args.kwargs["host_id"]
    database.close.assert_awaited_once()


async def wait_until(condition):
    async with asyncio.timeout(3):
        while not condition():
            await asyncio.sleep(0.005)


@pytest.fixture
def runtime(monkeypatch):
    result = SimpleNamespace(
        database=SimpleNamespace(close=AsyncMock()),
        flow_engine=SimpleNamespace(close=AsyncMock()),
        storage=object(),
        readiness=AsyncMock(return_value={"status": "ready", "checks": {"database": "ok"}}),
        observations=[],
    )

    async def publish(self, state):
        result.observations.append(state)

    monkeypatch.setattr(WorkerHealth, "publish", publish)
    monkeypatch.setattr(worker, "build_runtime", lambda _: result)
    monkeypatch.setattr(worker, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(worker, "HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(worker, "register_workflows", lambda: None)

    async def executor(runtime, stop, *, ready, health):
        result.health = health
        ready.set()
        try:
            await stop.wait()
        finally:
            ready.clear()

    monkeypatch.setattr(worker, "run_executor", executor)
    return result


async def test_heartbeats_continue_while_starting_unready_and_recovering(tmp_path, runtime):
    entered, release, stop = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def initial_check():
        entered.set()
        await release.wait()
        return {"status": "ready", "checks": {"database": "ok"}}

    runtime.readiness.side_effect = initial_check
    settings = Settings(
        config_file=None,
        flow_enabled=False,
        file_coordination_root=tmp_path,
        file_storages={"local": {"driver": "filesystem", "root": tmp_path}},
    )
    task = asyncio.create_task(worker.run(settings, stop))
    try:
        await entered.wait()
        await wait_until(lambda: len(runtime.observations) >= 2)
        assert set(runtime.observations) == {"starting"}
        release.set()
        await wait_until(lambda: runtime.observations[-1] == "ready")
        runtime.readiness.side_effect = None
        runtime.readiness.return_value = {"status": "unavailable", "checks": {"database": "bad"}}
        await wait_until(lambda: runtime.observations[-1] == "unready")
        count = len(runtime.observations)
        await wait_until(lambda: len(runtime.observations) >= count + 2)
        assert runtime.observations[-1] == "unready"
        runtime.readiness.return_value = {"status": "ready", "checks": {"database": "ok"}}
        await wait_until(lambda: runtime.observations[-1] == "ready")
        runtime.health.require_task("files")
        runtime.health.checked_task("files", False)
        await wait_until(lambda: runtime.observations[-1] == "ready")
        runtime.health.checked_task("files", True)
        await wait_until(lambda: runtime.observations[-1] == "ready")
    finally:
        release.set()
        stop.set()
        await asyncio.wait_for(task, 3)
    assert runtime.observations[-1] == "stopped"
    runtime.database.close.assert_awaited_once()


@pytest.mark.parametrize("flow_enabled", [False, True])
async def test_executor_readiness_and_failure_are_observed(monkeypatch, runtime, flow_enabled):
    entered, initialized, failure, stop = (asyncio.Event() for _ in range(4))

    async def fake_executor(runtime, stop, *, ready, health):
        entered.set()
        try:
            await initialized.wait()
            ready.set()
            await failure.wait()
            raise RuntimeError("executor stopped")
        finally:
            ready.clear()

    monkeypatch.setattr(worker, "run_executor", fake_executor)
    task = asyncio.create_task(
        worker.run(Settings(config_file=None, flow_enabled=flow_enabled), stop)
    )
    try:
        await entered.wait()
        await wait_until(lambda: len(runtime.observations) >= 2)
        assert "ready" not in runtime.observations
        initialized.set()
        await wait_until(lambda: runtime.observations[-1] == "ready")
    finally:
        failure.set()
        initialized.set()
        with pytest.raises(RuntimeError, match="executor stopped"):
            await asyncio.wait_for(task, 3)
    assert runtime.observations[-1] == "stopped"
    runtime.database.close.assert_awaited_once()
    assert runtime.flow_engine.close.await_count == int(flow_enabled)


async def test_stale_maintenance_does_not_restart_an_idle_process(tmp_path, monkeypatch, runtime):
    monkeypatch.setattr(worker_health, "MAX_AGE_SECONDS", 0.05)
    stop = asyncio.Event()
    settings = Settings(
        config_file=None,
        flow_enabled=False,
        file_coordination_root=tmp_path,
        file_storages={"local": {"driver": "filesystem", "root": tmp_path}},
    )
    task = asyncio.create_task(worker.run(settings, stop))
    try:
        await wait_until(lambda: "ready" in runtime.observations)

        runtime.health.require_task("files")
        runtime.health.checked_task("files", True)
        await asyncio.sleep(0.06)
        assert runtime.observations[-1] == "ready"
        count = len(runtime.observations)
        await wait_until(lambda: len(runtime.observations) >= count + 2)
    finally:
        stop.set()
        await asyncio.wait_for(task, 3)
    assert runtime.observations[-1] == "stopped"


async def test_failed_heartbeat_write_retries_without_exposing_driver_details(monkeypatch, caplog):
    stop = asyncio.Event()
    health = WorkerHealth(None)
    health.checked(True)
    attempts = []

    async def publish(state):
        attempts.append(state)
        if len(attempts) == 1:
            raise RuntimeError("password=hidden-heartbeat-secret")
        stop.set()

    health.publish = publish
    monkeypatch.setattr(worker, "HEARTBEAT_SECONDS", 0.01)
    await asyncio.wait_for(worker.report_health(health, stop, lambda: True), 3)
    assert attempts == ["ready", "ready"]
    assert "worker_heartbeat_failed" in caplog.text
    assert "hidden-heartbeat-secret" not in caplog.text


async def test_stalled_identity_connection_stops_executor(monkeypatch, runtime):
    async def stalled():
        await asyncio.Event().wait()

    runtime.worker_lease = stalled
    monkeypatch.setattr(worker, "LEASE_CHECK_SECONDS", 0.02)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(worker.run(Settings(config_file=None), asyncio.Event()), 1)
    assert runtime.observations[-1] == "stopped"
    runtime.database.close.assert_awaited_once()

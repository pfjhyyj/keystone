import asyncio
import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from keystone.core.config import Settings
from keystone.core.worker_health import WorkerHealth
from keystone.entrypoints import worker
from keystone.platform.flow import worker as flow_worker


def test_marker_freshness_and_invalid_paths(tmp_path):
    path = tmp_path / "worker.ready"
    health = WorkerHealth(path)
    assert not health.healthy() and not WorkerHealth(None).healthy()
    health.refresh()
    assert health.healthy()
    for timestamp in (time.time() - 31, time.time() + 60):
        os.utime(path, (timestamp, timestamp))
        assert not health.healthy()
    health.clear()
    assert not path.exists()
    assert not WorkerHealth(tmp_path).healthy()


@pytest.mark.parametrize("ready", [False, True])
def test_probe_exit_code_without_runtime_or_database(tmp_path, monkeypatch, capsys, ready):
    path = tmp_path / "worker.ready"
    if ready:
        path.touch()
    monkeypatch.setattr("sys.argv", ["keystone-worker", "--check"])
    monkeypatch.setattr(
        worker, "Settings", lambda: Settings(_env_file=None, worker_ready_file=path)
    )
    monkeypatch.setattr(worker, "build_runtime", lambda _: pytest.fail("Probe started runtime"))
    with pytest.raises(SystemExit) as result:
        worker.main()
    assert result.value.code == (0 if ready else 1)
    assert capsys.readouterr().out.strip() == ("Worker healthy" if ready else "Worker unavailable")
    assert path.exists() == ready  # Probes never refresh or create the marker.


async def wait_until(condition):
    async with asyncio.timeout(3):
        while not condition():
            await asyncio.sleep(0.01)


@pytest.fixture
def runtime(monkeypatch):
    result = SimpleNamespace(
        database=SimpleNamespace(close=AsyncMock()),
        flow_engine=SimpleNamespace(close=AsyncMock()),
        storage=object(),
        readiness=AsyncMock(return_value={"status": "ready", "checks": {"database": "ok"}}),
    )
    monkeypatch.setattr(worker, "build_runtime", lambda _: result)
    monkeypatch.setattr(worker, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(worker, "cleanup", AsyncMock())
    monkeypatch.setattr(worker, "register_workflows", lambda: None)
    return result


async def test_maintenance_marker_tracks_dependencies_failure_recovery_and_stop(
    tmp_path, monkeypatch, runtime
):
    path = tmp_path / "worker.ready"
    path.touch()  # A previous process left a fresh file; startup must invalidate it.
    health = WorkerHealth(path)
    entered, release, stop = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def initial_check():
        entered.set()
        await release.wait()
        return {"status": "ready", "checks": {"database": "ok"}}

    runtime.readiness.side_effect = initial_check
    settings = Settings(
        _env_file=None,
        worker_ready_file=path,
        file_coordination_root=tmp_path,
        file_storages={"local": {"driver": "filesystem", "root": tmp_path}},
    )
    task = asyncio.create_task(worker.run(settings, stop))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert not path.exists()
        release.set()
        await wait_until(health.healthy)
        runtime.readiness.side_effect = None
        runtime.readiness.return_value = {"status": "unavailable", "checks": {"database": "bad"}}
        await wait_until(lambda: not path.exists())
        runtime.readiness.return_value = {"status": "ready", "checks": {"database": "ok"}}
        await wait_until(health.healthy)
        worker.cleanup.side_effect = RuntimeError("cleanup failed")
        await wait_until(lambda: not path.exists())
        worker.cleanup.side_effect = None
        await wait_until(health.healthy)
    finally:
        release.set()
        stop.set()
        await asyncio.wait_for(task, 3)
    assert not path.exists()
    runtime.database.close.assert_awaited_once()


async def test_flow_marker_waits_for_initialization_and_clears_on_failure(
    tmp_path, monkeypatch, runtime
):
    path = tmp_path / "worker.ready"
    health = WorkerHealth(path)
    entered, initialized, failure, stop = (asyncio.Event() for _ in range(4))

    async def fake_flow(runtime, stop, *, ready):
        entered.set()
        try:
            await initialized.wait()
            ready.set()
            await failure.wait()
            raise RuntimeError("executor stopped")
        finally:
            ready.clear()

    monkeypatch.setattr(flow_worker, "run", fake_flow)
    settings = Settings(_env_file=None, worker_ready_file=path, flow_enabled=True)
    task = asyncio.create_task(worker.run(settings, stop))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        await wait_until(lambda: runtime.readiness.await_count >= 2)
        assert not path.exists()
        initialized.set()
        await wait_until(health.healthy)
    finally:
        initialized.set()
        failure.set()
        with pytest.raises(RuntimeError, match="executor stopped"):
            await asyncio.wait_for(task, 3)
    assert not path.exists()
    runtime.flow_engine.close.assert_awaited_once()
    runtime.database.close.assert_awaited_once()


async def test_unwritable_marker_stops_worker_and_closes_resources(tmp_path, runtime):
    path = tmp_path / "missing-parent" / "worker.ready"
    settings = Settings(_env_file=None, worker_ready_file=path)
    with pytest.raises(OSError):
        await worker.run(settings, asyncio.Event())
    assert not path.exists()
    runtime.database.close.assert_awaited_once()

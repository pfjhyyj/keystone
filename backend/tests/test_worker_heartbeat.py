import time
from datetime import timedelta

import pytest
from sqlalchemy import func, select, update
from test_files_dictionary import client as client
from test_identity import query

from keystone.core.models import WorkerHeartbeat, WorkerRegistration
from keystone.core.worker_health import WorkerHealth, worker_status
from keystone.entrypoints.worker import probe
from keystone.platform.flow.catalog import execution_version

pytestmark = pytest.mark.postgres


def observation(client):
    runtime = client.app.state.runtime
    health = WorkerHealth(runtime.database, runtime.settings, execution_version())
    health.claimed = True  # Synthetic observations, not a running executor.
    for name in ("files", "flow", "model_costs"):
        health.checked_task(name, True)

    async def registered(session):
        row = await session.get(WorkerRegistration, health.worker_id)
        if row is None:
            row = WorkerRegistration(worker_id=health.worker_id, state="registered")
            session.add(row)
        row.instance_id = health.instance_id
        row.execution_version, row.config_digest = health.application_version, health.digest
        row.stopped_at = None

    query(client, registered)
    return health


def test_worker_status_readiness_expiry_and_shutdown_never_change_api_readiness(client):
    health = observation(client)

    def status():
        return client.get("/api/v1/worker/status").json()

    assert status()["status"] == "offline" and status()["last_heartbeat_at"] is None
    baseline = client.get("/api/v1/ready").json()
    assert baseline["status"] == "ready"
    for observed, expected in (("starting", "unready"), ("ready", "ready"), ("unready", "unready")):
        client.portal.call(health.publish, observed)
        result = status()
        assert result["expected_workers"] == 1 and len(result["instances"]) == 1
        assert result["status"] == expected and result["last_heartbeat_at"]
        assert client.get("/api/v1/ready").json() == baseline
    client.portal.call(health.publish, "ready")
    assert client.portal.call(probe, client.app.state.runtime.settings)

    async def expire(session):
        await session.execute(
            update(WorkerHeartbeat).values(
                heartbeat_at=func.clock_timestamp() - timedelta(seconds=31)
            )
        )

    query(client, expire)
    assert status()["status"] == "offline"
    assert status()["last_heartbeat_at"]
    client.portal.call(health.publish, "ready")
    assert status()["status"] == "ready"
    client.portal.call(health.publish, "stopped")
    assert status()["status"] == "offline" and status()["last_heartbeat_at"]
    assert client.get("/api/v1/ready").status_code == 200


def test_late_old_worker_shutdown_does_not_hide_live_replacement_and_probe_is_host_local(client):
    old, current = observation(client), observation(client)
    current.host_id = "other-host-never-expose"
    client.portal.call(old.publish, "unready")
    client.portal.call(current.publish, "ready")
    result = client.get("/api/v1/worker/status")
    assert result.json()["status"] == "ready" and current.host_id not in result.text
    assert not client.portal.call(probe, client.app.state.runtime.settings)
    client.portal.call(old.publish, "stopped")
    assert client.get("/api/v1/worker/status").json()["status"] == "ready"
    stopped = query(client, lambda session: session.get(WorkerHeartbeat, old.instance_id))
    old.checked_task("files", False)
    client.portal.call(old.publish, "stopped")
    client.portal.call(old.publish, "ready")
    unchanged = query(client, lambda session: session.get(WorkerHeartbeat, old.instance_id))
    assert unchanged.state == "stopped"
    assert unchanged.heartbeat_at == stopped.heartbeat_at
    assert unchanged.task_results == stopped.task_results


def test_heartbeat_retention_and_unavailable_database(client):
    old = observation(client)
    client.portal.call(old.publish, "ready")

    async def age(session):
        await session.execute(
            update(WorkerHeartbeat).values(
                state="stopped", heartbeat_at=func.clock_timestamp() - timedelta(days=2)
            )
        )

    query(client, age)
    current = observation(client)
    client.portal.call(current.publish, "ready")
    assert query(
        client, lambda session: session.scalars(select(WorkerHeartbeat.instance_id))
    ).all() == [current.instance_id]
    assert client.portal.call(worker_status, None)["status"] == "unknown"


def test_shared_maintenance_failure_does_not_make_process_probe_fail(client):
    health = observation(client)
    health.checked_task("files", False)
    client.portal.call(health.publish, "ready")
    value = client.get("/api/v1/worker/status").json()
    assert value["status"] == "unready" and value["maintenance"]["files"]["status"] == "unready"
    assert value["instances"][0]["status"] == "ready"
    assert client.portal.call(probe, client.app.state.runtime.settings)

    async def expire_checks(session):
        row = await session.get(WorkerHeartbeat, health.instance_id)
        row.task_results = {
            name: {"healthy": True, "checked_at": "2000-01-01T00:00:00+00:00"}
            for name in row.task_results
        }

    query(client, expire_checks)
    assert (
        client.get("/api/v1/worker/status").json()["maintenance"]["model_costs"]["status"]
        == "unready"
    )


def test_delayed_heartbeat_does_not_freshen_old_maintenance_result(client):
    health = observation(client)
    health.task_checks["model_costs"] = (True, time.monotonic() - 35)
    client.portal.call(health.publish, "ready")
    value = client.get("/api/v1/worker/status").json()
    assert value["instances"][0]["status"] == "ready"
    assert value["maintenance"]["model_costs"]["status"] == "unready"
    previous = value["maintenance"]["model_costs"]["last_checked_at"]
    client.portal.call(health.publish, "ready")
    assert (
        client.get("/api/v1/worker/status").json()["maintenance"]["model_costs"]["last_checked_at"]
        == previous
    )


def test_late_ready_report_cannot_undo_draining(client):
    health = observation(client)
    client.portal.call(health.publish, "ready")
    health.state = "draining"
    health.checked(True)
    assert health.observation() == "draining"
    client.portal.call(health.publish, "draining")
    client.portal.call(health.publish, "ready")
    assert query(client, lambda s: s.get(WorkerHeartbeat, health.instance_id)).state == "draining"
    client.portal.call(health.publish, "stopped")
    assert query(client, lambda s: s.get(WorkerHeartbeat, health.instance_id)).state == "stopped"

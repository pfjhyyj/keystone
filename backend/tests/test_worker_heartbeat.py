from datetime import timedelta

import pytest
from sqlalchemy import func, select, update
from test_files_dictionary import client as client
from test_identity import query

from keystone.core.models import WorkerHeartbeat
from keystone.core.worker_health import WorkerHealth, worker_status
from keystone.entrypoints.worker import probe

pytestmark = pytest.mark.postgres


def test_worker_status_readiness_expiry_and_shutdown_never_change_api_readiness(client):
    database = client.app.state.runtime.database
    health = WorkerHealth(database)

    def status():
        return client.get("/api/v1/worker/status").json()

    assert status() == {"status": "offline", "last_heartbeat_at": None}
    baseline = client.get("/api/v1/ready").json()
    assert baseline["status"] == "ready"
    for observed, expected in (("starting", "unready"), ("ready", "ready"), ("unready", "unready")):
        client.portal.call(health.publish, observed)
        result = status()
        assert set(result) == {"status", "last_heartbeat_at"}
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
    database = client.app.state.runtime.database
    old, current = WorkerHealth(database), WorkerHealth(database)
    current.host_id = "other-host-never-expose"
    client.portal.call(old.publish, "unready")
    client.portal.call(current.publish, "ready")
    result = client.get("/api/v1/worker/status")
    assert result.json()["status"] == "ready" and current.host_id not in result.text
    assert not client.portal.call(probe, client.app.state.runtime.settings)
    client.portal.call(old.publish, "stopped")
    assert client.get("/api/v1/worker/status").json()["status"] == "ready"


def test_heartbeat_retention_and_unavailable_database(client):
    database = client.app.state.runtime.database
    old = WorkerHealth(database)
    client.portal.call(old.publish, "ready")

    async def age(session):
        await session.execute(
            update(WorkerHeartbeat).values(heartbeat_at=func.clock_timestamp() - timedelta(days=2))
        )

    query(client, age)
    current = WorkerHealth(database)
    client.portal.call(current.publish, "ready")
    assert query(
        client, lambda session: session.scalars(select(WorkerHeartbeat.instance_id))
    ).all() == [current.instance_id]
    assert client.portal.call(worker_status, None) == {
        "status": "unknown",
        "last_heartbeat_at": None,
    }

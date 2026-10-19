import asyncio
import logging

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from keystone.bootstrap import create_app
from keystone.core.config import Settings
from keystone.entrypoints import cli, worker


def test_offline_recovery_requires_explicit_stop_confirmation(monkeypatch):
    def unexpected_settings():
        pytest.fail("Must reject before loading settings or connecting to a database")

    monkeypatch.setattr(cli, "Settings", unexpected_settings)
    for arguments in (
        ["keystone", "recover-model-calls"],
        ["keystone", "migrate", "--confirm-api-stopped"],
    ):
        monkeypatch.setattr("sys.argv", arguments)
        with pytest.raises(SystemExit) as rejected:
            cli.main()
        assert rejected.value.code == 2


def settings(**values):
    return Settings(_env_file=None, database_url=None, **values)


@pytest.mark.parametrize("failure", ["missing_storage", "driver_error"])
def test_cli_reports_safe_configuration_hint_without_raw_exception(monkeypatch, caplog, failure):
    def configuration():
        if failure == "driver_error":
            raise RuntimeError("password=hidden-cli-secret")
        return settings()

    monkeypatch.setattr(cli, "Settings", configuration)
    monkeypatch.setattr(cli, "configure_logging", lambda _: None)
    monkeypatch.setattr("sys.argv", ["keystone", "init-storage"])
    with pytest.raises(SystemExit) as rejected:
        cli.main()
    assert rejected.value.code == 1
    assert "hidden-cli-secret" not in caplog.text
    if failure == "missing_storage":
        assert "storage_unavailable" in caplog.text
        assert "KEYSTONE_FILE_STORAGES" in caplog.text
        assert ".env" in caplog.text
    else:
        assert "invalid_configuration_or_database" in caplog.text
        assert "detail=" in caplog.text


def test_liveness_does_not_imply_database_or_domain_readiness():
    with TestClient(create_app(settings())) as client:
        live = client.get("/api/v1/health")
        ready = client.get("/api/v1/ready")
    assert live.status_code == 200
    assert ready.status_code == 503
    assert ready.json()["checks"] == {
        "database": "unconfigured",
        "schema": "unavailable",
        "storage": "unconfigured",
    }
    assert set(ready.json()) == {
        "status",
        "checks",
        "storage_sources",
        "flow_enabled",
    }
    assert ready.json()["flow_enabled"] is True
    assert ready.json()["status"] == "unavailable"
    assert live.headers["cache-control"] == "no-store"
    assert ready.headers["cache-control"] == "no-store"


def test_errors_hide_input_and_exception_details_and_assign_request_id(caplog):
    app = create_app(settings())

    @app.get("/test/validate")
    def validate(number: int):
        return number

    @app.get("/test/raise")
    def fail():
        raise ValueError("password=hidden-test-secret")

    @app.get("/test/http")
    def http_fail():
        raise HTTPException(403, detail="hidden-test-secret")

    caplog.set_level(logging.ERROR)
    with TestClient(app) as client:
        for path, code in [
            ("/test/validate?number=hidden-test-secret", 422),
            ("/test/raise", 500),
            ("/test/http", 403),
            ("/missing", 404),
        ]:
            response = client.get(path, headers={"X-Request-ID": "untrusted-id"})
            assert response.status_code == code
            assert "hidden-test-secret" not in response.text
            assert response.headers["x-request-id"] == response.json()["request_id"]
            assert response.json()["request_id"] != "untrusted-id"
    assert "hidden-test-secret" not in caplog.text
    event = next(record for record in caplog.records if record.message.startswith("request_failed"))
    assert event.request_id
    assert event.exception[0]["type"] == "ValueError"
    assert event.exception[0]["frames"][-1]["function"] == "fail"


def test_settings_reject_non_postgres_without_exposing_secret():
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, database_url="sqlite:///hidden-test-secret")
    assert "hidden-test-secret" not in str(error.value)


@pytest.mark.asyncio
async def test_worker_stop_closes_database(monkeypatch):
    stopped = asyncio.Event()
    entered = asyncio.Event()
    closed = asyncio.Event()

    class FakeDatabase:
        async def close(self):
            closed.set()

    class FakeRuntime:
        database = FakeDatabase()

        async def readiness(self):
            entered.set()
            return {"status": "unavailable", "checks": {"database": "unconfigured"}}

    monkeypatch.setattr(worker, "build_runtime", lambda _: FakeRuntime())
    task = asyncio.create_task(worker.run(settings(flow_enabled=False), stopped))
    await asyncio.wait_for(entered.wait(), timeout=2)
    stopped.set()
    await asyncio.wait_for(task, timeout=2)
    assert closed.is_set()

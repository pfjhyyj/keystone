"""HTTP observations identify templates and outcomes without storing caller inputs."""

import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from keystone.bootstrap import create_app
from keystone.core.config import Settings
from keystone.core.http import RequestBoundary
from keystone.use_cases import PlatformStatus


@pytest.mark.asyncio
async def test_final_body_send_failure_is_an_interrupted_response(caplog):
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 201, "headers": []})
        await send({"type": "http.response.body", "body": b"done"})

    async def send(message):
        if message["type"] == "http.response.body":
            raise OSError("connection closed")

    caplog.set_level(logging.INFO, logger="keystone.core.http")
    with pytest.raises(RuntimeError, match="Response interrupted"):
        await RequestBoundary(app)({"type": "http", "method": "POST"}, None, send)
    record = next(record for record in caplog.records if record.msg == "http_request")
    assert record.diagnostic["status_code"] == 201
    assert record.diagnostic["outcome"] == "interrupted"


@pytest.mark.asyncio
async def test_status_probe_failures_remain_unknown():
    @asynccontextmanager
    async def access():
        yield None, None

    async def workers():
        return {"status": "ready"}

    async def missing():
        raise RuntimeError("must-not-leak")

    async def empty():
        return 0, None

    value = await PlatformStatus(
        SimpleNamespace(credential=object(), access=access),
        workers,
        {"failed": missing, "empty": empty},
    ).execute()
    assert value["backlog"]["failed"] == {
        "status": "unknown",
        "pending": None,
        "oldest_pending_at": None,
        "oldest_pending_age_seconds": None,
    }
    assert value["backlog"]["empty"]["pending"] == 0
    assert "must-not-leak" not in str(value)


def test_http_observations_never_include_paths_query_values_or_bodies(caplog):
    app = create_app(Settings(config_file=None))

    @app.post("/probe/{identifier}")
    async def probe(identifier: str):
        return {"ok": True}

    caplog.set_level(logging.INFO, logger="keystone.core.http")
    with TestClient(app) as client:
        response = client.post(
            "/probe/private-secret?key=query-secret", json={"body": "body-secret"}
        )
        client.get("/unknown-path-secret?token=query-secret")
    records = [record for record in caplog.records if record.msg == "http_request"]
    assert len(records) == 2
    observation = records[0].diagnostic
    assert observation["route"] == "/probe/{identifier}"
    assert observation["method"] == "POST" and observation["status_code"] == 200
    assert observation["outcome"] == "completed" and observation["elapsed_ms"] >= 0
    assert records[0].request_id == response.headers["x-request-id"]
    assert records[1].diagnostic["route"] == "<unmatched>"
    assert records[1].diagnostic["status_code"] == 404
    assert all("secret" not in str(record.diagnostic) for record in records)

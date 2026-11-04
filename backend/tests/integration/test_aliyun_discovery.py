"""Real database workspace persistence, discovery authorization and baseline constraints."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import httpx2
import pytest
from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from test_files_dictionary import client as client
from test_identity import bearer, token

from keystone.core.config import Settings
from keystone.core.migrations import migration_config

pytestmark = pytest.mark.postgres
BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"
SECRET = "inference-secret"


def create_channel(client, **changes):
    response = client.post(
        "/api/v1/model-channels",
        json={
            "name": "ali",
            "provider": "aliyun_bailian",
            "api_base": BASE,
            "api_key": SECRET,
            "billing_api_key": "not-the-inference-key",
            "workspace_id": "llm-original",
            **changes,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def edit_channel(client, row, **changes):
    return client.patch(
        f"/api/v1/model-channels/{row['id']}",
        json={
            "name": row["name"],
            "api_base": row["api_base"],
            **changes,
        },
    )


def native_result():
    return {
        "success": True,
        "code": None,
        "output": {
            "total": 2,
            "page_size": 100,
            "page_no": 1,
            "models": [{"model": "qwen-plus", "prices": "private"}, {"model": "qwen-max"}],
        },
    }


def install(client, monkeypatch, handler):
    monkeypatch.setattr(
        client.app.state.runtime.models.http, "transport", lambda _: httpx2.MockTransport(handler)
    )


def test_workspace_configuration_and_listing_require_management_and_do_not_import_models(
    client, monkeypatch
):
    row = create_channel(client)
    calls = []

    def respond(request):
        calls.append(request)
        assert request.url.host == "llm-original.cn-beijing.maas.aliyuncs.com"
        assert request.url.path == "/api/v1/models"
        assert request.headers["authorization"] == "Bearer " + SECRET
        return httpx2.Response(200, json=native_result())

    install(client, monkeypatch, respond)
    path = f"/api/v1/model-channels/{row['id']}/models"
    headers = bearer(token(client, ["model:invoke"])["token"])
    assert client.get(path, headers=headers).status_code == 403 and not calls
    response = client.get(path)
    assert response.status_code == 200, response.text
    assert response.json() == {"items": [{"id": "qwen-max"}, {"id": "qwen-plus"}]}
    assert len(calls) == 1
    assert client.get("/api/v1/model-entries").json() == []
    assert client.get("/api/v1/model-invocations").json() == []
    assert SECRET not in client.get("/api/v1/audit-logs").text
    assert edit_channel(client, row, name="renamed").json()["workspace_id"] == "llm-original"
    changed = edit_channel(client, row, workspace_id="llm-next").json()
    assert changed["workspace_id"] == "llm-next" and changed["api_base"] == BASE
    assert edit_channel(client, row, workspace_id=None).json()["workspace_id"] is None
    install(client, monkeypatch, lambda request: httpx2.Response(200, json=native_result()))
    assert client.get(path).status_code == 200


def test_workspace_is_ali_only_and_invalid_values_rollback_without_side_effects(client):
    row = create_channel(client)
    for workspace in ["evil.invalid", "a/b", "-a", "a-", "x" * 64]:
        assert edit_channel(client, row, workspace_id=workspace).status_code == 422
    assert client.get("/api/v1/model-channels").json()[0]["workspace_id"] == "llm-original"
    other = client.post(
        "/api/v1/model-channels",
        json={
            "name": "other",
            "provider": "openrouter",
            "api_base": "https://openrouter.ai/api/v1",
            "api_key": SECRET,
            "workspace_id": "llm-other",
        },
    )
    assert other.status_code == 422
    assert len(client.get("/api/v1/model-channels").json()) == 1


@pytest.mark.parametrize("change", ["workspace", "key", "revoke"])
def test_discovery_discards_results_after_workspace_key_change_or_revocation(
    client, monkeypatch, change
):
    row = create_channel(client)
    credential = token(client, ["model:manage"])
    entered, release = Event(), Event()

    async def respond(request):
        entered.set()
        assert await asyncio.to_thread(release.wait, 10)
        return httpx2.Response(200, json=native_result())

    install(client, monkeypatch, respond)
    with ThreadPoolExecutor() as executor:
        pending = executor.submit(
            client.get,
            f"/api/v1/model-channels/{row['id']}/models",
            headers=bearer(credential["token"]),
        )
        try:
            assert entered.wait(5)
            if change == "workspace":
                assert edit_channel(client, row, workspace_id="llm-new").status_code == 200
            elif change == "key":
                assert edit_channel(client, row, api_key="new-key").status_code == 200
            else:
                assert client.delete(f"/api/v1/auth/tokens/{credential['id']}").status_code == 204
        finally:
            release.set()
        response = pending.result(timeout=10)
    assert response.status_code == (401 if change == "revoke" else 409)
    assert "qwen-plus" not in response.text


def test_workspace_baseline_defaults_and_constraints(empty_database_url):
    config = migration_config(Settings(config_file=None, database_url=empty_database_url))
    config.attributes["create_schema"] = True
    command.upgrade(config, "0005")
    engine = create_engine(empty_database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text("""INSERT INTO keystone.model_channels
                (id,name,provider,api_base,api_key_encrypted,is_active,balance_check_status)
                VALUES ('00000000-0000-4000-8000-000000000099','legacy','aliyun_bailian',
                    'https://dashscope.aliyuncs.com/compatible-mode/v1','opaque-secret',true,'never')""")
            )
        with engine.begin() as connection:
            assert connection.execute(
                text("SELECT workspace_id,api_key_encrypted FROM keystone.model_channels")
            ).one() == (None, "opaque-secret")
            connection.execute(
                text("UPDATE keystone.model_channels SET workspace_id='llm-example'")
            )
        for update in ["workspace_id='evil.invalid'", "provider='openrouter'"]:
            with pytest.raises(IntegrityError), engine.begin() as connection:
                connection.execute(text("UPDATE keystone.model_channels SET " + update))
    finally:
        engine.dispose()

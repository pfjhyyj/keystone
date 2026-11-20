"""Real database workspace persistence and discovery authorization."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import httpx2
import pytest
from test_files_dictionary import client as client
from test_identity import bearer, token

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
    assert edit_channel(client, row, workspace_id="evil.invalid").status_code == 422
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
    assert other.json()["code"] == "model_workspace_invalid"
    assert len(client.get("/api/v1/model-channels").json()) == 1
    other_row = create_channel(
        client,
        name="other",
        provider="openrouter",
        api_base="https://openrouter.ai/api/v1",
        workspace_id=None,
    )
    rejected = edit_channel(client, other_row, workspace_id="llm-other")
    assert rejected.status_code == 422
    assert rejected.json()["code"] == "model_workspace_invalid"
    rows = {item["id"]: item for item in client.get("/api/v1/model-channels").json()}
    assert rows[other_row["id"]]["workspace_id"] is None
    assert rows[row["id"]]["workspace_id"] == "llm-original"


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

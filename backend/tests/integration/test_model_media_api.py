"""Direct media APIs: real authorization/ledger/Files with deterministic SDK HTTP."""

import json
from uuid import UUID

import httpx2
import pytest
from sqlalchemy import select
from support.model_stub import image_bytes
from support.relay_images import install
from test_files_dictionary import client as client
from test_files_dictionary import upload
from test_identity import assign, bearer, create_user, login, query, role, token
from test_model_access import channel, entry
from test_model_access import model_configuration as model_configuration

from keystone.platform.flow.models import FlowRun
from keystone.platform.model_access.adapters.http import HttpClient
from keystone.platform.model_access.models import ModelInvocation

pytestmark = pytest.mark.postgres
MEDIA_PATH = {
    "vision": "understand-image",
    "images/generate": "generate-image",
    "images/edit": "edit-image",
}


def configure(client, *, provider="openrouter", operations=None, key="media"):
    options = {} if provider == "openrouter" else {"api_base": "https://yunwu.ai/v1"}
    configured = channel(client, provider, **options)
    entry(
        client,
        configured,
        key=key,
        provider_model="mock-chat" if provider == "openrouter" else "gpt-image-2",
        operations=operations or ["image.generate", "image.edit", "image.understand"],
    )


def source(client):
    response = upload(client, image_bytes(), content_type="image/png", name="reference.png")
    assert response.status_code == 201, response.text
    return {"file_id": response.json()["id"]}


def calls(client):
    return query(client, lambda s: s.scalars(select(ModelInvocation))).all()


def test_catalog_limits_follow_requested_operation_for_the_same_model(client):
    configure(client, provider="yunwu")
    for operation, count, maximum, parameters in [
        ("image.generate", 9, 32, ["aspect_ratio", "quality"]),
        ("image.edit", 9, 32, ["aspect_ratio", "quality"]),
        ("image.understand", 4, 8, []),
    ]:
        response = client.get("/api/v1/_test/model-options", params={"operation": operation})
        assert response.status_code == 200
        options = response.json()["items"][0]
        assert options["max_reference_images"] == count
        assert options["max_input_bytes"] == maximum * 1024 * 1024
        assert options["image_parameters"] == parameters
    assert calls(client) == []


@pytest.mark.parametrize("operation", ["generate", "edit", "vision"])
def test_direct_media_records_one_call_without_a_flow(client, operation):
    configure(client)
    body = {"model_key": "media", "prompt": "private-input-do-not-persist", "n": 2}
    if operation in {"edit", "vision"}:
        body["images"] = [source(client)]
    if operation == "vision":
        body.pop("prompt")
        body.pop("n")
        body["messages"] = [{"role": "user", "content": "private-vision-input"}]
    path = "vision" if operation == "vision" else f"images/{operation}"
    response = client.post(f"/api/v1/_test/{MEDIA_PATH[path]}", json=body)
    assert response.status_code == 200, response.text
    result = response.json()
    rows = calls(client)
    assert len(rows) == 1 and str(rows[0].id) == result["invocation_id"]
    assert rows[0].status == "succeeded" and rows[0].flow_context is None
    assert rows[0].worker_id is None and rows[0].cost_amount is not None
    assert (
        rows[0].operation
        == {"generate": "image.generate", "edit": "image.edit", "vision": "image.understand"}[
            operation
        ]
    )
    assert query(client, lambda s: s.scalar(select(FlowRun.id))) is None
    assert "private-" not in str(rows[0].result_context)
    if operation == "vision":
        assert "模拟渠道响应" in result["content"]
    else:
        assert len(result["files"]) == 2
        for item in result["files"]:
            image = client.get(f"/api/v1/_test/materials/{item['file_id']}/content")
            assert image.status_code == 200 and image.content == image_bytes()
        public = client.get(f"/api/v1/model-invocations/{result['invocation_id']}").json()
        assert "result_context" not in public


@pytest.mark.parametrize("kind", ["url", "base64"])
def test_direct_relay_uses_original_result_and_publishes_files(client, monkeypatch, tmp_path, kind):
    configure(client, provider="yunwu", operations=["image.generate", "image.edit"])
    log = tmp_path / "requests.jsonl"
    original = HttpClient.transport
    monkeypatch.setattr(HttpClient, "transport", original)
    install(log, kind=kind, retry=True)
    result = client.post(
        "/api/v1/_test/edit-image",
        json={
            "model_key": "media",
            "prompt": "blue",
            "images": [source(client)],
            "aspect_ratio": "3:4",
            "quality": "medium",
        },
    )
    assert result.status_code == 200, result.text
    records = [json.loads(line) for line in log.read_text().splitlines()]
    assert sum(r["method"] == "POST" for r in records) == 1
    assert all(not r["auth"] for r in records if r["method"] == "GET" and "auth" in r)
    assert len(calls(client)) == 1 and calls(client)[0].status == "succeeded"
    file = result.json()["files"][0]["file_id"]
    assert client.get(f"/api/v1/_test/materials/{file}/content").content == image_bytes()
    options = client.get("/api/v1/_test/model-options?operation=image.edit").json()["items"][0]
    assert options["image_parameters"] == ["aspect_ratio", "quality"]
    assert options["max_reference_images"] == 9


@pytest.mark.parametrize(
    "permissions,path",
    [
        (["model:invoke"], "images/generate"),
        (["test_files.asset:create"], "images/generate"),
        (["model:invoke", "test_files.asset:create"], "images/edit"),
    ],
)
def test_missing_functional_permissions_never_send(client, permissions, path):
    configure(client)
    body = {"model_key": "media", "prompt": "blue"}
    if path.endswith("edit"):
        body["images"] = [source(client)]
    headers = bearer(token(client, permissions)["token"])
    assert (
        client.post(f"/api/v1/_test/{MEDIA_PATH[path]}", json=body, headers=headers).status_code
        == 403
    )
    assert calls(client) == []


def test_foreign_file_is_not_authorized_by_model_permission(client):
    configure(client)
    reference = source(client)
    user = create_user(client)
    permissions = role(
        client,
        "media-user",
        ["model:invoke", "test_files.asset:content_read", "test_files.asset:create"],
    )
    assign(client, user["id"], permissions["id"])
    login(client, "alice")
    client.post(
        "/api/v1/auth/password",
        json={"current_password": "test-password-123", "password": "changed-password-123"},
    )
    login(client, "alice", "changed-password-123")
    response = client.post(
        "/api/v1/_test/understand-image",
        json={
            "model_key": "media",
            "messages": [{"role": "user", "content": "describe"}],
            "images": [reference],
        },
    )
    assert response.status_code == 404
    assert calls(client) == []


def test_invalid_input_and_unsupported_operation_never_register(client):
    configure(client, operations=["image.generate"])
    for path, body, status in [
        ("images/edit", {"prompt": "blue", "images": [source(client)]}, 409),
        ("images/generate", {"prompt": "blue", "n": 3}, 422),
        ("images/generate", {"prompt": "blue", "quality": "high"}, 422),
    ]:
        response = client.post(
            f"/api/v1/_test/{MEDIA_PATH[path]}", json={"model_key": "media", **body}
        )
        assert response.status_code == status, response.text
    assert calls(client) == []


@pytest.mark.parametrize("case", ["mislabeled", "truncated"])
def test_invalid_vision_file_bytes_are_rejected_before_registration(client, monkeypatch, case):
    configure(client, operations=["image.understand"])
    content = image_bytes()
    response = upload(
        client,
        content[:20] if case == "truncated" else content,
        content_type="text/plain" if case == "mislabeled" else "image/png",
        name="input.png",
    )
    assert response.status_code == 201, response.text
    file_id = response.json()["id"]

    def forbidden(request):
        pytest.fail("Invalid file bytes reached the upstream")

    monkeypatch.setattr(
        client.app.state.runtime.models.http, "transport", lambda _: httpx2.MockTransport(forbidden)
    )
    result = client.post(
        "/api/v1/_test/understand-image",
        json={
            "model_key": "media",
            "images": [{"file_id": file_id}],
            "messages": [{"role": "user", "content": "Describe"}],
        },
    )
    assert result.status_code == 422 and result.json()["code"] == "model_invalid_image"
    assert calls(client) == []
    assert query(client, lambda session: session.scalar(select(FlowRun.id))) is None


def test_read_without_upload_permission_and_create_without_inspect(client):
    configure(client)
    reference = source(client)
    headers = bearer(token(client, ["model:invoke", "test_files.asset:content_read"])["token"])
    result = client.post(
        "/api/v1/_test/understand-image",
        headers=headers,
        json={
            "model_key": "media",
            "images": [reference],
            "messages": [{"role": "user", "content": "describe"}],
        },
    )
    assert result.status_code == 200, result.text
    headers = bearer(token(client, ["model:invoke", "test_files.asset:create"])["token"])
    result = client.post(
        "/api/v1/_test/generate-image",
        headers=headers,
        json={"model_key": "media", "prompt": "blue"},
    )
    assert result.status_code == 200, result.text
    file = result.json()["files"][0]["file_id"]
    assert client.get(f"/api/v1/_test/materials/{file}", headers=headers).status_code == 403


def test_download_failure_preserves_generation_success_without_resending(client, monkeypatch):
    configure(client, provider="yunwu", operations=["image.generate"])
    sends = []

    def transport(self, provider):
        async def respond(request):
            sends.append(request.method)
            if request.method == "POST":
                return httpx2.Response(
                    200,
                    headers={"x-api-request-id": "original"},
                    json={"data": [{"url": "https://cdn.example.test/image.png"}]},
                )
            return httpx2.Response(404)

        return httpx2.MockTransport(respond)

    monkeypatch.setattr(HttpClient, "transport", transport)
    response = client.post(
        "/api/v1/_test/generate-image", json={"model_key": "media", "prompt": "blue"}
    )
    assert response.status_code == 502, response.text
    assert sends.count("POST") == 1
    row = calls(client)[0]
    assert row.status == "succeeded" and row.upstream_request_id == "original"
    assert row.result_context["kind"] == "download"
    assert "cdn.example" not in str(row.result_context)


def test_direct_result_evidence_cannot_be_read_by_another_actor(client):
    from keystone.core.errors import ServiceError
    from keystone.platform.identity.schemas.credentials import Credential
    from keystone.platform.identity.services.access import AccessContext
    from keystone.platform.model_access.schemas.images import ImageGenerateRequest
    from keystone.platform.model_access.services.entries import ModelEntryService
    from keystone.platform.model_access.services.runtime import ModelRuntime

    configure(client)
    response = client.post(
        "/api/v1/_test/generate-image", json={"model_key": "media", "prompt": "blue"}
    )
    identifier = UUID(response.json()["invocation_id"])
    user = create_user(client)
    assigned = role(client, "invoke-only", ["model:invoke"])
    assign(client, user["id"], assigned["id"])
    login(client, "alice")
    client.post(
        "/api/v1/auth/password",
        json={"current_password": "test-password-123", "password": "changed-password-123"},
    )
    login(client, "alice", "changed-password-123")
    secret = token(client, ["model:invoke"])["token"]

    async def inspect():
        identity = AccessContext(
            client.app.state.runtime.database,
            client.app.state.runtime.settings,
            Credential("pat", secret),
        )
        entries = ModelEntryService(identity, client.app.state.runtime.models)
        selected = await entries.selection("media", operation="image.generate")
        with pytest.raises(ServiceError, match="原图片调用无法恢复"):
            await ModelRuntime(entries).image_manifest(
                identifier,
                data=ImageGenerateRequest(prompt="blue"),
                selection=selected,
                context=None,
            )

    client.portal.call(inspect)


@pytest.mark.parametrize(
    "provider,base",
    [
        ("openlux", "https://api.openlux.ai/v1"),
        ("yunwu", "https://yunwu.ai/v1"),
        ("yunwu", "https://relay.example/gateway/v1"),
        ("newapi", "https://newapiai.com/v1"),
        ("newapi", "https://relay.example/gateway/v1"),
        ("newapi", "https://api.openlux.ai/v1"),
    ],
)
def test_compatible_vision_catalog_and_send_match_explicit_model_operations(
    client,
    monkeypatch,
    provider,
    base,
):
    configured = channel(client, provider, api_base=base)
    entry(client, configured, key="vision", operations=["image.understand"])
    entry(client, configured, key="text-only", operations=["text.generate"])
    rows = client.get("/api/v1/_test/model-options?operation=image.understand").json()["items"]
    assert [row["key"] for row in rows] == ["vision"]
    assert (
        client.get("/api/v1/_test/model-options?operation=image.understand&delivery=async").json()[
            "items"
        ]
        == []
    )
    sent = []
    original = HttpClient.transport

    def transport(self, kind):
        underlying = original(self, kind)

        async def respond(request):
            if request.method == "POST":
                payload = json.loads(request.content)
                sent.append(str(request.url))
                assert payload["messages"][-1]["content"][0]["image_url"]["url"].startswith(
                    "data:image/png;base64,"
                )
            return await underlying.handle_async_request(request)

        return httpx2.MockTransport(respond)

    monkeypatch.setattr(HttpClient, "transport", transport)
    request = {
        "model_key": "vision",
        "images": [source(client)],
        "messages": [{"role": "user", "content": "describe"}],
    }
    denied = client.post(
        "/api/v1/_test/understand-image", json={**request, "model_key": "text-only"}
    )
    assert denied.status_code == 409 and calls(client) == []
    response = client.post("/api/v1/_test/understand-image", json=request)
    assert response.status_code == 200, response.text
    assert "模拟渠道响应" in response.json()["content"]
    assert sent == [base + "/chat/completions"]
    assert len(calls(client)) == 1 and calls(client)[0].operation == "image.understand"

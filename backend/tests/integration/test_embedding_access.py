"""Actual SDK/transport/ledger boundary over isolated PostgreSQL, no network transport."""

import json
from uuid import uuid4

import httpx2
import pytest
from test_files_dictionary import client as client
from test_identity import query
from test_model_access import channel, entry
from test_model_access import model_configuration as model_configuration

from keystone.core.errors import ServiceError
from keystone.platform.identity.schemas.credentials import Credential
from keystone.platform.identity.services.access import AccessContext
from keystone.platform.model_access.adapters.http import HttpClient
from keystone.platform.model_access.models import ModelInvocation
from keystone.platform.model_access.schemas.embeddings import EmbeddingRequest
from keystone.platform.model_access.services.entries import ModelEntryService
from keystone.platform.model_access.services.runtime import ModelRuntime

pytestmark = pytest.mark.postgres


def runtime(client):
    rt = client.app.state.runtime
    identity = AccessContext(
        rt.database, rt.settings, Credential("session", client.cookies["keystone_session"])
    )
    return ModelRuntime(ModelEntryService(identity, rt.models))


def mock_embedding(monkeypatch, *, invalid=False):
    previous, calls = HttpClient.transport, []

    def transport(self, provider):
        fallback = previous(self, provider)

        async def respond(request):
            if not request.url.path.endswith("/embeddings"):
                return await fallback.handle_async_request(request)
            body = json.loads(request.content)
            calls.append(body)
            return httpx2.Response(
                200,
                json={
                    "id": "mock-embedding",
                    "object": "list",
                    "model": body["model"],
                    "data": [
                        {
                            "object": "embedding",
                            "index": index,
                            "embedding": [0.0] * body["dimensions"]
                            if invalid
                            else [float(i + 1) for i in range(body["dimensions"])],
                        }
                        for index, _ in enumerate(body["input"])
                    ],
                    "usage": {"prompt_tokens": 9, "total_tokens": 9, "cost": 0.0002},
                },
            )

        return httpx2.MockTransport(respond)

    monkeypatch.setattr(HttpClient, "transport", transport)
    return calls


@pytest.mark.parametrize(
    "provider,base",
    [
        ("openrouter", "https://openrouter.ai/api/v1"),
        ("newapi", "https://newapiai.com/v1"),
    ],
)
def test_embedding_sdk_preserves_native_receipts_and_refuses_duplicate_send(
    client, monkeypatch, provider, base
):
    ordinary = entry(client, channel(client, provider=provider, api_base=base))
    entry(client, {"id": ordinary["channel_id"]}, key="embed", operations=["text.embed"])
    assert client.get("/api/v1/models?operation=text.embed").json()["items"][0]["key"] == "embed"
    assert client.get("/api/v1/model-entries").json()[0]["operations"] == ["text.generate"]
    calls, model, identifier = mock_embedding(monkeypatch), runtime(client), uuid4()

    async def exercise():
        selected = await model.service.selection("embed", operation="text.embed")
        data = EmbeddingRequest(texts=["景色", "动作"], dimensions=3)
        assert await model.invoke_embedding(
            data, selection=selected, invocation_id=identifier, context=None, worker_identity=None
        ) == [[1, 2, 3], [1, 2, 3]]
        with pytest.raises(ServiceError) as error:
            await model.invoke_embedding(
                data,
                selection=selected,
                invocation_id=identifier,
                context=None,
                worker_identity=None,
            )
        assert error.value.code == "model_result_unavailable"

    client.portal.call(exercise)
    assert len(calls) == 1 and calls[0]["encoding_format"] == "float"
    record = query(client, lambda session: session.get(ModelInvocation, identifier))
    assert record.operation == "text.embed" and record.status == "succeeded"
    assert record.usage == {"input_tokens": 9, "total_tokens": 9}
    assert record.result_context is None
    if provider == "openrouter":
        assert record.cost_source == "response"


def test_embedding_wire_rejects_extra_fields_before_registration(client, monkeypatch):
    entry(client, channel(client), key="embed", operations=["text.embed"])
    calls, model, identifier = mock_embedding(monkeypatch), runtime(client), uuid4()

    async def exercise():
        selected = await model.service.selection("embed", operation="text.embed")

        async def request(sdk):
            return await sdk.embeddings.create(
                model=selected.provider_model,
                input=["x"],
                dimensions=3,
                encoding_format="float",
                extra_body={"messages": []},
            )

        with pytest.raises(ServiceError) as error:
            await model.invoke_sdk(
                selection=selected, invocation_id=identifier, sdk_request=request
            )
        assert error.value.code == "model_invalid_request"

    client.portal.call(exercise)
    assert calls == [] and query(client, lambda s: s.get(ModelInvocation, identifier)) is None


def test_invalid_embedding_vector_retains_attempt_and_cannot_be_resent(client, monkeypatch):
    entry(client, channel(client), key="embed", operations=["text.embed"])
    calls, model, identifier = mock_embedding(monkeypatch, invalid=True), runtime(client), uuid4()

    async def exercise():
        selected = await model.service.selection("embed", operation="text.embed")
        for _ in range(2):
            with pytest.raises(ServiceError):
                await model.invoke_embedding(
                    EmbeddingRequest(texts=["x"], dimensions=3),
                    selection=selected,
                    invocation_id=identifier,
                    context=None,
                    worker_identity=None,
                )

    client.portal.call(exercise)
    assert len(calls) == 1
    record = query(client, lambda s: s.get(ModelInvocation, identifier))
    assert record.status == "unknown" and record.cost_source == "response"

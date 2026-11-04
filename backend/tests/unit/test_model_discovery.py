from uuid import uuid4

import httpx2
import pytest

from keystone.core.errors import ServiceError
from keystone.platform.model_access.adapters.billing.registry import BillingClient
from keystone.platform.model_access.adapters.channels import CHANNELS
from keystone.platform.model_access.adapters.http import HttpClient
from keystone.platform.model_access.adapters.inference.client import InferenceClient
from keystone.platform.model_access.adapters.inference.transport import Observation
from keystone.platform.model_access.schemas import Invoke, Selection
from keystone.platform.model_access.schemas.channels import ChannelCreate


@pytest.mark.parametrize("provider", ["openlux", "openrouter", "yunwu", "newapi", "volcengine"])
async def test_discovery_uses_fixed_get_with_inference_key_and_projects_only_ids(provider):
    calls = []

    def respond(request):
        calls.append(request)
        assert request.method == "GET"
        assert str(request.url) == CHANNELS[provider].api_base + "/models"
        assert request.headers["Authorization"] == "Bearer private-key"
        return httpx2.Response(
            200,
            json={
                "data": [
                    {"id": "z-model", "secret_metadata": "must-not-return"},
                    {"id": "a-model"},
                    {"id": "z-model"},
                ]
            },
        )

    http = HttpClient()
    http.transport = lambda _: httpx2.MockTransport(respond)
    sender = InferenceClient(http)
    assert await sender.list_models(provider, CHANNELS[provider].api_base, "private-key") == [
        {"id": "a-model"},
        {"id": "z-model"},
    ]
    if provider not in {"newapi", "yunwu"}:
        with pytest.raises(ServiceError) as rejected:
            await sender.list_models(provider, "https://untrusted.invalid/v1", "private-key")
        assert rejected.value.code == "model_target_not_allowed"
    assert len(calls) == 1


@pytest.mark.parametrize(
    "payload,provider",
    [
        ([], "openlux"),
        ({}, "openlux"),
        ({"data": {}}, "openlux"),
        ({"data": [None]}, "openlux"),
        ({"data": [{"id": 1}]}, "openlux"),
        ({"data": [{"id": ""}]}, "openlux"),
        ({"data": [{"id": "x" * 129}]}, "openlux"),
        ({"data": [{"id": "has\ncontrol"}]}, "openlux"),
        ({"data": [{"id": "a b"}]}, "openlux"),
        ({"data": [{"id": "x"}] * 10001}, "openlux"),
        ([], "volcengine"),
    ],
)
async def test_discovery_rejects_invalid_and_unbounded_model_lists(payload, provider):
    http = HttpClient()
    http.transport = lambda _: httpx2.MockTransport(lambda _: httpx2.Response(200, json=payload))
    with pytest.raises(ServiceError) as error:
        await InferenceClient(http).list_models(provider, CHANNELS[provider].api_base, "secret")
    assert error.value.code == "model_invalid_response"


@pytest.mark.parametrize(
    "status,code,provider",
    [
        (401, "model_authentication_failed", "openlux"),
        (403, "model_access_denied", "openlux"),
        (429, "model_rate_limited", "openlux"),
        (302, "model_query_failed", "openlux"),
        (500, "model_query_failed", "openlux"),
        (401, "model_authentication_failed", "volcengine"),
    ],
)
async def test_discovery_sanitizes_failures_without_retry_or_redirect(status, code, provider):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx2.Response(
            status, text="secret upstream body", headers={"location": "https://evil.invalid"}
        )

    http = HttpClient()
    http.transport = lambda _: httpx2.MockTransport(respond)
    with pytest.raises(ServiceError) as error:
        await InferenceClient(http).list_models(provider, CHANNELS[provider].api_base, "secret")
    assert error.value.code == code
    assert "secret" not in error.value.detail
    assert len(calls) == 1


def test_newapi_defaults_to_newapiai_and_resolves_verified_site_capabilities():
    adapter = CHANNELS["newapi"]
    sender = InferenceClient(None)
    selected = Selection(
        model_entry_id=uuid4(),
        channel_id=uuid4(),
        provider="newapi",
        api_base="https://newapiai.com/v1",
        provider_model="chat",
    )
    assert sender.request_target("newapi", selected) == (
        "https://newapiai.com/v1",
        "/chat/completions",
        "model",
    )
    assert adapter.models_path == "/models"
    assert BillingClient(None).adapter("newapi").binding(selected.api_base).mode == "logs"
    for base in ("https://api.openlux.ai/v1", "https://yunwu.ai/v1"):
        assert sender.request_target("newapi", selected.model_copy(update={"api_base": base})) == (
            base,
            "/chat/completions",
            "model",
        )
        assert BillingClient(None).adapter("newapi", base).cost_query_mode == "logs"
        sender.check_operation("newapi", "image.generate", api_base=base)
    sender.check_operation("newapi", "image.understand")
    with pytest.raises(ServiceError):
        sender.check_operation("newapi", "image.generate")


async def test_custom_newapi_prefix_and_hostname_are_preserved_for_discovery_and_send():
    base = "https://relay.example:8443/gateway/api/v1"
    calls, registered = [], []

    async def respond(request):
        calls.append(request)
        assert request.url.host == "relay.example" and request.url.port == 8443
        assert request.headers["host"] == "relay.example:8443"
        assert request.headers["authorization"] == "Bearer private-key"
        if request.method == "GET":
            assert request.url.path == "/gateway/api/v1/models"
            return httpx2.Response(200, json={"data": [{"id": "chat"}]})
        assert request.url.path == "/gateway/api/v1/chat/completions"
        return httpx2.Response(
            200,
            json={
                "id": "safe-id",
                "model": "chat",
                "created": 0,
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "answer"},
                    }
                ],
            },
        )

    http = HttpClient()
    http.transport = lambda _: httpx2.MockTransport(respond)
    sender = InferenceClient(http)
    assert await sender.list_models("newapi", base, "private-key") == [{"id": "chat"}]

    async def before_send():
        registered.append(True)
        return "private-key"

    async def receipt():
        pass

    selected = Selection(
        model_entry_id=uuid4(),
        channel_id=uuid4(),
        provider="newapi",
        api_base=base,
        provider_model="chat",
    )
    events = [
        event
        async for event in sender.events(
            "newapi",
            selected,
            Observation(lambda _: None),
            before_send,
            receipt,
            uuid4(),
            data=Invoke(model_key="chat", messages=[{"role": "user", "content": "private"}]),
        )
    ]
    assert events[-1]["content"] == "answer"
    assert len(calls) == 2 and registered == [True]


@pytest.mark.parametrize("host", ["10.0.0.1", "newapi.x-era.com"])
async def test_newapi_accepts_configured_hosts_without_public_ip_preflight(monkeypatch, host):
    import socket

    # A Fake-IP resolver must not prevent reaching the configured transport.
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args: [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("198.18.1.236", 443))],
    )
    calls = []

    def respond(request):
        calls.append(request)
        assert request.url.host == host
        assert request.headers["authorization"] == "Bearer private-key"
        return httpx2.Response(200, json={"data": [{"id": "chat"}]})

    http = HttpClient()
    http.transport = lambda _: httpx2.MockTransport(respond)
    assert await InferenceClient(http).list_models(
        "newapi", f"https://{host}/v1", "private-key"
    ) == [{"id": "chat"}]
    assert len(calls) == 1


def test_api_base_normalization_preserves_prefix_and_removes_only_trailing_slash():
    channel = ChannelCreate(
        name="custom",
        provider="newapi",
        api_key="secret",
        api_base="https://Relay.EXAMPLE:443/gateway/API/v1/",
    )
    assert channel.api_base == "https://relay.example/gateway/API/v1"

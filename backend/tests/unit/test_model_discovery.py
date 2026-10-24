from uuid import uuid4

import httpx2
import pytest

from keystone.core.errors import ServiceError
from keystone.platform.model_access.adapters.billing.registry import BillingClient
from keystone.platform.model_access.adapters.channels import CHANNELS
from keystone.platform.model_access.adapters.http import HttpClient
from keystone.platform.model_access.adapters.inference.client import InferenceClient
from keystone.platform.model_access.schemas import Selection


@pytest.mark.parametrize("provider", ["openlux", "openrouter", "yunwu", "newapi"])
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
    with pytest.raises(ServiceError) as rejected:
        await sender.list_models(provider, "https://untrusted.invalid/v1", "private-key")
    assert rejected.value.code == "model_target_not_allowed"
    assert len(calls) == 1


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"data": {}},
        {"data": [None]},
        {"data": [{"id": 1}]},
        {"data": [{"id": ""}]},
        {"data": [{"id": "x" * 129}]},
        {"data": [{"id": "has\ncontrol"}]},
        {"data": [{"id": "a b"}]},
        {"data": [{"id": "x"}] * 10001},
    ],
)
async def test_discovery_rejects_invalid_and_unbounded_model_lists(payload):
    http = HttpClient()
    http.transport = lambda _: httpx2.MockTransport(lambda _: httpx2.Response(200, json=payload))
    with pytest.raises(ServiceError) as error:
        await InferenceClient(http).list_models("openlux", CHANNELS["openlux"].api_base, "secret")
    assert error.value.code == "model_invalid_response"


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "model_authentication_failed"),
        (403, "model_access_denied"),
        (429, "model_rate_limited"),
        (302, "model_query_failed"),
        (500, "model_query_failed"),
    ],
)
async def test_discovery_sanitizes_failures_without_retry_or_redirect(status, code):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx2.Response(
            status, text="secret upstream body", headers={"location": "https://evil.invalid"}
        )

    http = HttpClient()
    http.transport = lambda _: httpx2.MockTransport(respond)
    with pytest.raises(ServiceError) as error:
        await InferenceClient(http).list_models("openlux", CHANNELS["openlux"].api_base, "secret")
    assert error.value.code == code
    assert "secret" not in error.value.detail
    assert len(calls) == 1


def test_newapi_is_a_separate_site_with_text_and_discovery_only():
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
    assert BillingClient(None).adapter("newapi").binding(selected.api_base) is None
    for base in ("https://api.openlux.ai/v1", "https://yunwu.ai/v1"):
        with pytest.raises(ServiceError):
            sender.request_target("newapi", selected.model_copy(update={"api_base": base}))
    for operation in ("image.generate", "image.understand"):
        with pytest.raises(ServiceError):
            sender.check_operation("newapi", operation)

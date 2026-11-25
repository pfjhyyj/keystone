"""Installed channel compositions share protocols without sharing billing semantics."""

import json
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import httpx2
import pytest

from keystone.core.errors import ServiceError
from keystone.platform.model_access.adapters.billing.registry import BillingClient
from keystone.platform.model_access.adapters.http import HttpClient
from keystone.platform.model_access.adapters.inference.client import InferenceClient
from keystone.platform.model_access.adapters.inference.transport import Observation
from keystone.platform.model_access.channels import CHANNELS
from keystone.platform.model_access.channels.shared.billing.contracts import BillingQueryError
from keystone.platform.model_access.schemas.errors import AdapterError
from keystone.platform.model_access.schemas.invocations import Selection, TextRequest
from keystone.platform.model_access.schemas.structured import (
    JsonOutput,
    StructuredRequest,
    parse_structured,
)
from keystone.platform.model_access.services.entries import catalog_entry


def token_limit(provider):
    # The official API rejects the legacy name on reasoning models; relays keep it.
    return "max_completion_tokens" if provider == "openai" else "max_tokens"


@pytest.mark.parametrize(
    "provider,operation,delivery,model,supported",
    [
        ("openrouter", "text.structured", "sync", "chat", True),
        ("openrouter", "image.edit", "sync", "image", True),
        ("openrouter", "image.generate", "async", "image", False),
        ("openlux", "image.generate", "async", "kling-v2-1", True),
        ("openlux", "image.generate", "async", "other", False),
        ("openlux", "image.generate", "sync", "kling-v2-1", False),
        ("newapi", "text.structured", "async", "chat", False),
        ("yunwu", "image.generate", "sync", "image", False),
        ("deepseek", "text.generate", "sync", "chat", False),
        ("aliyun_bailian", "text.embed", "sync", "chat", True),
        ("volcengine", "text.embed", "sync", "chat", False),
        ("openai", "text.structured", "sync", "chat", True),
        ("openai", "text.embed", "sync", "embedding", True),
        ("openai", "image.edit", "sync", "gpt-image-2", True),
        # Official image models are declared separately from relay-only aliases.
        ("openai", "image.generate", "sync", "gpt-image-2-c", False),
        ("openai", "image.generate", "async", "gpt-image-2", False),
        ("unknown", "text.generate", "sync", "chat", False),
        ("openrouter", "video.generate", "async", "video", False),
    ],
)
def test_catalog_and_sender_use_the_same_channel_capabilities(
    provider, operation, delivery, model, supported
):
    sender = InferenceClient(None)
    row = SimpleNamespace(key="entry", operations=[operation], provider_model=model)
    # The public catalog projects a supported operation with its implementation's delivery.
    catalog = catalog_entry(row, provider)["operations"]
    adapter = CHANNELS.get(provider)
    expected_catalog = bool(adapter and adapter.protocol(operation, model=model))
    assert (operation in catalog) is expected_catalog
    if supported:
        sender.check_operation(provider, operation, delivery, model=model)
        protocol = adapter.protocol(operation, delivery, model=model)
        selected = SimpleNamespace(
            provider=provider,
            api_base=adapter.api_base,
            provider_model=model,
            operation=operation,
            delivery=delivery,
        )
        target = sender.request_target(provider, selected)
        assert protocol.paths[target[1]] == "POST"
    else:
        with pytest.raises(ServiceError) as error:
            sender.check_operation(provider, operation, delivery, model=model)
        assert error.value.code in {"model_provider_unavailable", "model_operation_unavailable"}


@pytest.mark.parametrize(
    "provider,custom_auth",
    [
        ("openrouter", False),
        ("openlux", False),
        ("yunwu", False),
        ("yunwu", True),
        ("aliyun_bailian", False),
        ("volcengine", False),
        ("openai", False),
    ],
)
async def test_shared_chat_wire_keeps_channel_receipts_and_actual_cost_semantics(
    monkeypatch, provider, custom_auth
):
    calls, receipts = [], []
    billing = BillingClient(None)
    adapter = CHANNELS[provider]
    selected = Selection(
        model_entry_id=uuid4(),
        channel_id=uuid4(),
        provider_model="chat",
        provider=provider,
        api_base=adapter.api_base,
    )
    registered = False
    authorizations = []

    def header_auth(request, secret):
        assert registered
        # The SDK's placeholder never reaches the upstream under a custom auth scheme.
        assert "Authorization" not in request.headers
        request.headers["X-Test-Key"] = secret
        authorizations.append(True)

    channels = dict(CHANNELS)
    if custom_auth:
        channels[provider] = replace(adapter, authorize=header_auth)

    async def before_send():
        nonlocal registered
        registered = True
        return "private-secret"

    async def on_receipt():
        assert registered

    def respond(request):
        assert registered
        if custom_auth:
            assert "Authorization" not in request.headers
            assert request.headers["X-Test-Key"] == "private-secret"
        else:
            assert request.headers["Authorization"] == "Bearer private-secret"
        assert str(request.url) == adapter.api_base + "/chat/completions"
        body = json.loads(request.content)
        assert body["model"] == "chat" and body[token_limit(provider)] == 1024
        assert set(body) & {"max_tokens", "max_completion_tokens"} == {token_limit(provider)}
        if provider == "aliyun_bailian":
            assert body["enable_thinking"] is False
        calls.append(body)
        return httpx2.Response(
            200,
            headers={"x-api-request-id": "billing-id"},
            json={
                "id": "response-id",
                "model": "chat",
                "created": 1770000000,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 2,
                    "total_tokens": 5,
                    "cost": "0.0012",
                },
            },
        )

    http = HttpClient()
    monkeypatch.setattr(http, "transport", lambda _: httpx2.MockTransport(respond))
    observation = Observation(
        lambda value: receipts.append(
            billing.adapter(provider).receipt(value.receipt_ids, value.reported_cost)
        )
    )
    result = [
        event
        async for event in InferenceClient(http, channels).events(
            provider,
            selected,
            observation,
            before_send,
            on_receipt,
            uuid4(),
            data=TextRequest(messages=[{"role": "user", "content": "hello"}]),
        )
    ]
    assert result == [{"type": "result", "content": "ok"}] and len(calls) == 1
    assert len(authorizations) == int(custom_auth)
    assert observation.usage == {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5}
    final = receipts[-1]
    if provider == "openrouter":
        assert final.request_id == "response-id" and final.fee.record("response-id") == {
            "request_id": "response-id",
            "amount": "0.0012",
            "unit": "credits",
        }
    elif provider in {"aliyun_bailian", "volcengine", "openai"}:
        assert final.request_id == "response-id" and final.fee is None
    else:
        assert final.request_id == "billing-id" and final.fee is None


@pytest.mark.parametrize(
    "provider",
    ["openrouter", "openlux", "yunwu", "newapi", "aliyun_bailian", "volcengine", "openai"],
)
async def test_structured_wire_registers_once_and_keeps_each_channel_contract(
    monkeypatch, provider
):
    base = (
        "https://relay.example/gateway/v1" if provider == "newapi" else CHANNELS[provider].api_base
    )
    selected = Selection(
        model_entry_id=uuid4(),
        channel_id=uuid4(),
        provider=provider,
        api_base=base,
        provider_model="structured-model",
        operation="text.structured",
    )
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "integer"}},
        "required": ["answer"],
        "additionalProperties": False,
    }
    data = StructuredRequest(
        messages=[{"role": "user", "content": "Answer"}],
        output=JsonOutput(name="Answer", json_schema=schema),
    )
    registrations, requests, receipts = [], [], []

    async def register():
        registrations.append(True)
        return "private-structured-key"

    async def receipt():
        receipts.append(True)

    def respond(request):
        assert registrations == [True]
        assert request.method == "POST" and str(request.url) == base + "/chat/completions"
        assert request.headers["authorization"] == "Bearer private-structured-key"
        body = json.loads(request.content)
        if provider == "aliyun_bailian":
            assert body["enable_thinking"] is False
        assert body["model"] == "structured-model" and body[token_limit(provider)] == 1024
        assert set(body) & {"max_tokens", "max_completion_tokens"} == {token_limit(provider)}
        assert body["messages"] == [{"role": "user", "content": "Answer"}]
        assert body["response_format"] == {
            "type": "json_schema",
            "json_schema": {"name": "Answer", "strict": True, "schema": schema},
        }
        assert body.get("provider", {}).get("require_parameters", False) is (
            provider == "openrouter"
        )
        requests.append(body)
        return httpx2.Response(
            200,
            headers={"x-api-request-id": "original-billing-receipt"},
            json={
                "id": "structured-response",
                "model": "structured-model",
                "created": 1770000000,
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": '{"answer":7}'},
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            },
        )

    http = HttpClient()
    monkeypatch.setattr(http, "transport", lambda _: httpx2.MockTransport(respond))
    observation = Observation(lambda _: None)
    events = [
        event
        async for event in InferenceClient(http).events(
            provider, selected, observation, register, receipt, uuid4(), data=data
        )
    ]
    assert len(events) == len(requests) == len(receipts) == 1
    assert parse_structured(events[0]["content"], data.output) == {"answer": 7}
    assert observation.receipt_ids["x-api-request-id"] == "original-billing-receipt"
    assert observation.usage == {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5}


@pytest.mark.parametrize(
    "provider,operation,required",
    [
        ("openrouter", "text.structured", True),
        ("openrouter", "text.generate", False),
        ("openlux", "text.generate", False),
        ("aliyun_bailian", "text.generate", False),
        ("aliyun_bailian", "text.structured", False),
        ("volcengine", "text.structured", False),
    ],
)
def test_agent_settings_come_from_channel_code_and_preserve_caller_preferences(
    provider, operation, required
):
    selected = Selection(
        model_entry_id=uuid4(),
        channel_id=uuid4(),
        provider_model="chat",
        provider=provider,
        api_base=CHANNELS[provider].api_base,
        operation=operation,
    )
    original = {"max_tokens": 32, "extra_body": {"provider": {"allow_fallbacks": False}}}
    result = InferenceClient(None).sdk_settings(selected, original)
    if provider == "aliyun_bailian":
        assert result["extra_body"]["enable_thinking"] is False
    assert result["max_tokens"] == 32
    preferences = result["extra_body"]["provider"]
    assert preferences["allow_fallbacks"] is False
    assert preferences.get("require_parameters", False) is required
    assert original == {"max_tokens": 32, "extra_body": {"provider": {"allow_fallbacks": False}}}


async def test_protocol_errors_are_translated_only_at_the_host_boundary():
    adapter = CHANNELS["openrouter"].billing
    with pytest.raises(AdapterError) as raw:
        adapter.check_target("https://wrong.invalid", "/credits")
    with pytest.raises(ServiceError) as public:
        await BillingClient(None).balance("openrouter", "https://wrong.invalid", "secret")
    assert (public.value.status, public.value.code, public.value.detail) == (
        raw.value.status,
        raw.value.code,
        raw.value.detail,
    )
    assert "secret" not in str(public.value)
    with pytest.raises(BillingQueryError) as unsupported:
        await BillingClient(None).page("openrouter", {}, "secret")
    assert unsupported.value.reason == "cost_query_unavailable"
    with pytest.raises(ServiceError) as unknown:
        BillingClient(None).adapter("unknown")
    assert unknown.value.code == "model_provider_unavailable"


def structured_images_request(count=2, max_tokens=8000):
    from keystone.platform.model_access.schemas.structured import StructuredRequest

    return StructuredRequest(
        messages=[{"role": "user", "content": "inspect"}],
        images=[{"file_id": uuid4()} for _ in range(count)],
        output=JsonOutput(
            name="Answer",
            json_schema={
                "type": "object",
                "properties": {"answer": {"type": "integer"}},
                "required": ["answer"],
                "additionalProperties": False,
            },
        ),
        max_tokens=max_tokens,
    )


@pytest.mark.parametrize(
    "provider", ["openrouter", "openai", "yunwu", "newapi", "aliyun_bailian", "volcengine"]
)
async def test_structured_images_share_the_vision_wire_and_strict_schema(monkeypatch, provider):
    from support.model_stub import image_bytes

    base = (
        "https://relay.example/gateway/v1" if provider == "newapi" else CHANNELS[provider].api_base
    )
    selected = Selection(
        model_entry_id=uuid4(),
        channel_id=uuid4(),
        provider=provider,
        api_base=base,
        provider_model="vision-model",
        operation="text.structured",
    )
    data = structured_images_request()
    registrations, bodies = [], []

    async def register():
        registrations.append(True)
        return "secret"

    async def receipt():
        pass

    def respond(request):
        assert registrations == [True]
        # Above the former 4096 bound the host waits up to 600 s for slow decoding.
        assert request.extensions["timeout"]["read"] == 600
        bodies.append(json.loads(request.content))
        return httpx2.Response(
            200,
            json={
                "id": "structured-vision",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": '{"answer":7}'},
                    }
                ],
            },
        )

    http = HttpClient()
    monkeypatch.setattr(http, "transport", lambda _: httpx2.MockTransport(respond))
    client = InferenceClient(http)
    images = [("image/png", image_bytes(), "auto")] * 2

    async def send(sdk):
        return await client.vision(sdk, selected, data, images)

    events = [
        event
        async for event in client.events(
            provider,
            selected,
            Observation(lambda _: None),
            register,
            receipt,
            uuid4(),
            sdk_request=send,
            image_count=2,
            output_tokens=data.max_tokens,
        )
    ]
    assert parse_structured(events[0]["content"], data.output) == {"answer": 7}
    [body] = bodies
    assert body[token_limit(provider)] == 8000
    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "Answer", "strict": True, "schema": data.output.json_schema},
    }
    assert body.get("provider", {}).get("require_parameters", False) is (provider == "openrouter")
    parts = body["messages"][-1]["content"]
    assert [part["image_url"]["url"][:22] for part in parts] == ["data:image/png;base64,"] * 2


@pytest.mark.parametrize("case", ["no_vision_protocol", "count", "remote", "undeclared", "text"])
async def test_structured_image_mismatches_are_rejected_before_registration(monkeypatch, case):
    from keystone.platform.model_access.channels.shared import CHAT

    channels = dict(CHANNELS)
    if case == "no_vision_protocol":
        text_only = replace(CHAT, operations=frozenset({"text.generate", "text.structured"}))
        channels["openrouter"] = replace(CHANNELS["openrouter"], protocols=(text_only,))
    selected = Selection(
        model_entry_id=uuid4(),
        channel_id=uuid4(),
        provider="openrouter",
        api_base=CHANNELS["openrouter"].api_base,
        provider_model="vision-model",
        operation="text.generate" if case == "text" else "text.structured",
    )
    url = "data:image/png;base64,AAAA" if case != "remote" else "https://cdn.invalid/a.png"
    parts = [{"type": "image_url", "image_url": {"url": url}}]
    body = {
        "model": "vision-model",
        "messages": [{"role": "user", "content": parts * (1 if case == "count" else 2)}],
        "max_tokens": 1024,
        "provider": {"require_parameters": True},
    }
    request = httpx2.Request(
        "POST", selected.api_base + "/chat/completions", content=json.dumps(body)
    )
    client = InferenceClient(HttpClient(), channels)
    expected = {
        "no_vision_protocol": "model_operation_unavailable",
        "count": "model_invalid_request",
        "remote": "model_invalid_request",
        "undeclared": "model_operation_unavailable",
        "text": "model_operation_unavailable",
    }[case]
    with pytest.raises(ServiceError) as error:
        # The host declares the approved count; images without it stay text-only.
        client.validate_request(
            request, "openrouter", selected, image_count=0 if case == "undeclared" else 2
        )
    assert error.value.code == expected
    if case == "no_vision_protocol":
        with pytest.raises(ServiceError) as error:
            client.check_structured_images(selected)
        assert error.value.code == "model_operation_unavailable"


def test_structured_image_and_output_bounds():
    from pydantic import ValidationError

    from keystone.platform.model_access.schemas.invocations import MAX_OUTPUT_TOKENS

    assert MAX_OUTPUT_TOKENS == 16384
    assert structured_images_request(count=4, max_tokens=16384).max_tokens == 16384
    for count, tokens in ((5, 1024), (1, 16385), (1, 0)):
        with pytest.raises(ValidationError):
            structured_images_request(count=count, max_tokens=tokens)
    assert structured_images_request(count=0).images == []


async def test_structured_image_bytes_and_count_are_checked_before_any_send():
    from keystone.platform.model_access.services.runtime import ModelRuntime

    class Runtime(ModelRuntime):
        def _events(self, *args, **kwargs):
            pytest.fail("Invalid image input reached registration")

    service = SimpleNamespace(
        identity=SimpleNamespace(database=None, settings=None),
        clients=SimpleNamespace(inference=InferenceClient(None)),
    )
    runtime = Runtime(service)
    selected = Selection(
        model_entry_id=uuid4(),
        channel_id=uuid4(),
        provider="openrouter",
        api_base=CHANNELS["openrouter"].api_base,
        provider_model="vision-model",
        operation="text.structured",
    )
    data = structured_images_request(count=2)
    oversized = [("image/png", b"x" * (4 * 1024 * 1024 + 1), "auto")] * 2
    for images, code in (
        (oversized[:1], "model_invalid_request"),
        (oversized, "model_input_too_large"),
    ):
        with pytest.raises(ServiceError) as error:
            await runtime.invoke_text(
                data,
                images=images,
                selection=selected,
                invocation_id=uuid4(),
                context=None,
                worker_identity=None,
            )
        assert error.value.code == code

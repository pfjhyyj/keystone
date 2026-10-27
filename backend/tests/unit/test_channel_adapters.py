"""Installed channel compositions share protocols without sharing billing semantics."""

import ast
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx2
import pytest
from support.ast_support import imports

from keystone.core.errors import ServiceError
from keystone.platform.model_access.adapters.billing.contracts import BillingQueryError
from keystone.platform.model_access.adapters.billing.registry import BillingClient
from keystone.platform.model_access.adapters.channels import CHANNELS
from keystone.platform.model_access.adapters.http import HttpClient
from keystone.platform.model_access.adapters.inference.client import InferenceClient
from keystone.platform.model_access.adapters.inference.transport import Observation
from keystone.platform.model_access.schemas.errors import AdapterError
from keystone.platform.model_access.schemas.invocations import Selection, TextRequest
from keystone.platform.model_access.schemas.structured import (
    JsonOutput,
    StructuredRequest,
    parse_structured,
)
from keystone.platform.model_access.services.entries import catalog_entry


@pytest.mark.parametrize(
    "provider,operation,delivery,model,supported",
    [
        ("openrouter", "text.generate", "sync", "chat", True),
        ("openrouter", "text.structured", "sync", "chat", True),
        ("openrouter", "image.understand", "sync", "vision", True),
        ("openrouter", "image.generate", "sync", "image", True),
        ("openrouter", "image.edit", "sync", "image", True),
        ("openrouter", "image.generate", "async", "image", False),
        ("openlux", "text.generate", "sync", "chat", True),
        ("openlux", "image.understand", "sync", "vision", True),
        ("openlux", "image.generate", "async", "kling-v2-1", True),
        ("openlux", "image.generate", "async", "other", False),
        ("openlux", "image.generate", "sync", "kling-v2-1", False),
        ("openlux", "text.structured", "sync", "chat", True),
        ("yunwu", "text.structured", "sync", "chat", True),
        ("newapi", "text.structured", "sync", "chat", True),
        ("newapi", "text.structured", "async", "chat", False),
        ("yunwu", "text.generate", "sync", "chat", True),
        ("yunwu", "image.understand", "sync", "vision", True),
        ("newapi", "image.understand", "sync", "vision", True),
        ("newapi", "image.understand", "async", "vision", False),
        ("yunwu", "image.generate", "sync", "image", False),
        ("deepseek", "text.generate", "sync", "chat", False),
        ("aliyun_bailian", "text.generate", "sync", "chat", False),
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
    [("openrouter", False), ("openlux", False), ("yunwu", False), ("yunwu", True)],
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
        assert body["model"] == "chat" and body["max_tokens"] == 1024
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
    assert observation.request_budget.max_output_tokens == 1024
    final = receipts[-1]
    if provider == "openrouter":
        assert final.request_id == "response-id" and final.fee.record("response-id") == {
            "request_id": "response-id",
            "amount": "0.0012",
            "unit": "credits",
        }
    else:
        assert final.request_id == "billing-id" and final.fee is None


@pytest.mark.parametrize("provider", ["openrouter", "openlux", "yunwu", "newapi"])
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
        assert body["model"] == "structured-model" and body["max_tokens"] == 1024
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
    assert observation.request_budget.operation == "text.structured"
    assert observation.usage == {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5}


@pytest.mark.parametrize(
    "provider,operation,required",
    [
        ("openrouter", "text.structured", True),
        ("openrouter", "text.generate", False),
        ("openlux", "text.generate", False),
        ("openlux", "text.structured", False),
        ("yunwu", "text.structured", False),
        ("newapi", "text.structured", False),
        (None, "text.generate", False),
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
        api_base=CHANNELS[provider or "openrouter"].api_base,
        operation=operation,
    )
    original = {"max_tokens": 32, "extra_body": {"provider": {"allow_fallbacks": False}}}
    result = InferenceClient(None).sdk_settings(selected, original)
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


def test_protocol_implementations_do_not_import_host_or_execution_dependencies():
    root = Path(__file__).parents[2] / "src/keystone/platform/model_access"
    files = [root / "adapters/channels.py", root / "schemas/errors.py"]
    files += [
        root / "adapters/inference" / name
        for name in ("protocols.py", "images.py", "kling.py", "openai_images.py")
    ]
    files += [p for p in (root / "adapters/billing").glob("*.py") if p.name != "registry.py"]
    for path in files:
        package = "keystone.platform.model_access." + ".".join(path.relative_to(root).parts[:-1])
        dependencies = list(imports(ast.parse(path.read_text(encoding="utf8")), package))
        assert not any(
            name.startswith(
                (
                    "sqlalchemy",
                    "dbos",
                    "fastapi",
                    "keystone.core.errors",
                    "keystone.core.database",
                    "keystone.core.execution",
                    "keystone.composition",
                    "keystone.bootstrap",
                )
            )
            or ".services" in name
            or ".models" in name
            or ".workflows" in name
            or name.startswith("keystone.platform.identity")
            or name.endswith(("adapters.http", "adapters.coordination", "adapters.credentials"))
            for name in dependencies
        ), path

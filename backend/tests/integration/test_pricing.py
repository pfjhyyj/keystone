"""Real authorization, registration transactions and tariff revisions; stubbed paid I/O."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import replace
from decimal import Decimal
from threading import Event
from uuid import UUID, uuid4

import httpx2
import pytest
from support.pricing import config, image_rule, token_rule
from test_embedding_access import mock_embedding, runtime
from test_files_dictionary import client as client
from test_identity import bearer, query, token
from test_model_access import channel, entry, invoke
from test_model_access import model_configuration as model_configuration

from keystone.platform.audit.services import writer as audit
from keystone.platform.model_access.adapters.http import HttpClient
from keystone.platform.model_access.channels import CHANNELS
from keystone.platform.model_access.channels.aliyun_bailian.chat import DashScopeChat
from keystone.platform.model_access.channels.aliyun_bailian.pricing import BASE as ALI_BASE
from keystone.platform.model_access.channels.shared import CHAT, EMBEDDINGS
from keystone.platform.model_access.channels.volcengine.pricing import BASE as ARK_BASE
from keystone.platform.model_access.models import ModelInvocation
from keystone.platform.model_access.schemas.embeddings import EmbeddingRequest
from keystone.platform.model_access.schemas.structured import StructuredRequest

pytestmark = pytest.mark.postgres


@pytest.fixture(autouse=True)
def native_cloud_protocols(model_configuration, monkeypatch):
    # The legacy shared stub installs Ali as a generic chat-only relay. These tests
    # keep mocked HTTP but explicitly exercise the actual native protocol composition.
    monkeypatch.setitem(
        CHANNELS,
        "aliyun_bailian",
        replace(
            CHANNELS["aliyun_bailian"],
            api_base=ALI_BASE,
            protocols=(DashScopeChat(CHAT.operations), EMBEDDINGS),
        ),
    )
    monkeypatch.setitem(
        CHANNELS,
        "volcengine",
        replace(
            CHANNELS["volcengine"],
            api_base=ARK_BASE,
            protocols=(replace(CHAT, pricing_meter="volcengine_chat.v2"),),
        ),
    )


@pytest.mark.parametrize(
    "stream,provider,model,profile,expected",
    [
        (False, "aliyun_bailian", "qwen-plus", "chat.v3", "0.001136"),
        (
            True,
            "volcengine",
            "doubao-seed-2-0-pro-260215",
            "volcengine_chat.v2",
            "0.006144",
        ),
    ],
)
def test_native_reference_prices_use_normalized_cached_receipts_without_manual_configuration(
    client, monkeypatch, stream, provider, model, profile, expected
):
    entry(
        client,
        channel(client, provider, api_base=CHANNELS[provider].api_base),
        provider_model=model,
    )
    sent = []

    def transport(self, provider):
        async def respond(request):
            body = json.loads(request.content)
            sent.append(body)
            usage = {
                "input_tokens": 1000,
                "output_tokens": 200,
                "total_tokens": 1200,
                "input_tokens_details": {"cached_tokens": 100, "audio_tokens": None},
                "output_tokens_details": {"reasoning_tokens": None},
            }
            result = {
                "id": "priced-native",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "done"},
                    }
                ],
                "usage": usage,
            }
            if not stream:
                return httpx2.Response(200, json=result)
            chunks = [
                {
                    "id": "priced-native",
                    "choices": [{"index": 0, "delta": {"content": "done"}, "finish_reason": None}],
                },
                {
                    "id": "priced-native",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                },
                {"id": "priced-native", "choices": [], "usage": usage},
            ]
            content = b"".join(b"data: " + json.dumps(chunk).encode() + b"\n\n" for chunk in chunks)
            return httpx2.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=content + b"data: [DONE]\n\n",
            )

        return httpx2.MockTransport(respond)

    monkeypatch.setattr(HttpClient, "transport", transport)
    response = invoke(client, stream=stream)
    assert response.status_code == 200, response.text
    row = client.get("/api/v1/model-invocations").json()[0]
    assert row["status"] == "succeeded" and row["estimate_status"] == "estimated"
    assert Decimal(row["estimate_amount"]) == Decimal(expected)
    saved = stored(client, row["id"])
    assert saved.estimate_snapshot["revision"] == 0
    assert saved.estimate_facts["values"]["usage.input_uncached_tokens"]["value"] == 900
    assert saved.estimate_snapshot["profile"] == profile and len(sent) == 1
    if provider == "aliyun_bailian":
        assert sent[0]["enable_thinking"] is False
    else:
        assert saved.estimate_snapshot["context"]["service_tier"] == "default"


def price(client, model, value=None, revision=None):
    path = f"/api/v1/model-entries/{model['id']}/pricing"
    current = client.get(path).json()
    response = client.put(
        path,
        json={
            "config": value or config(),
            "expected_revision": current["revision"] if revision is None else revision,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def stored(client, identifier):
    return query(client, lambda session: session.get(ModelInvocation, UUID(str(identifier))))


@pytest.mark.parametrize(
    "provider,stream",
    [
        ("openrouter", False),
        ("newapi", True),
    ],
)
def test_sync_and_sse_share_the_frozen_calculator_without_changing_actuals(
    client, provider, stream
):
    model = entry(client, channel(client, provider, api_base=CHANNELS[provider].api_base))
    price(client, model)
    response = invoke(client, stream=stream)
    assert response.status_code == 200, response.text
    row = client.get("/api/v1/model-invocations").json()[0]
    assert row["status"] == "succeeded" and row["estimate_status"] == "estimated"
    assert Decimal(row["estimate_amount"]) == Decimal("0.000088")
    assert row["estimate_currency"] == "CNY" and row["pricing_revision"] == 1
    assert row["cost_source"] == ("response" if provider == "openrouter" else "unknown")
    assert "estimate_snapshot" not in row and "estimate_facts" not in row
    assert "private-prompt" not in json.dumps(row)
    assert CHANNELS[provider].api_base not in json.dumps(row)


def test_preview_is_manual_and_has_no_ledger_or_config_side_effects(client):
    model = entry(client, channel(client))
    path = f"/api/v1/model-entries/{model['id']}/pricing"
    original = client.get(path).json()
    response = client.post(
        path + "/preview",
        json={
            "config": config(),
            "operation": "text.generate",
            "facts": {
                "profile": original["operations"][0]["profile"]["id"],
                "values": {
                    "usage.input_total_tokens": {
                        "value": 1000,
                        "unit": "token",
                        "source": "manual",
                    },
                    "usage.output_total_tokens": {
                        "value": 200,
                        "unit": "token",
                        "source": "manual",
                    },
                },
            },
        },
    )
    assert response.status_code == 200, response.text
    assert Decimal(response.json()["amount"]) == Decimal("0.0036")
    assert client.get(path).json() == original
    assert client.get("/api/v1/model-invocations").json() == []
    assert client.get("/api/v1/audit-logs?action=model.pricing.update").json() == []


@pytest.mark.parametrize("method,suffix", [("GET", ""), ("PUT", ""), ("POST", "/preview")])
def test_pricing_management_respects_pat_intersection(client, method, suffix):
    model = entry(client, channel(client))
    headers = bearer(token(client, ["model:invoke"])["token"])
    body = (
        {"expected_revision": 0, "config": config()}
        if method == "PUT"
        else {"config": config(), "operation": "text.generate", "facts": {"profile": "chat.v2"}}
    )
    response = client.request(
        method,
        f"/api/v1/model-entries/{model['id']}/pricing{suffix}",
        headers=headers,
        **({"json": body} if method != "GET" else {}),
    )
    assert response.status_code == 403


def test_concurrent_edits_compare_revision_and_audit_only_hashes(client):
    model = entry(client, channel(client))
    path = f"/api/v1/model-entries/{model['id']}/pricing"
    with ThreadPoolExecutor(2) as pool:
        pending = [
            pool.submit(client.put, path, json={"expected_revision": 0, "config": config()})
            for _ in range(2)
        ]
        responses = [result.result(timeout=10) for result in pending]
    assert sorted(r.status_code for r in responses) == [200, 409]
    assert client.get(path).json()["revision"] == 1
    logs = client.get("/api/v1/audit-logs?action=model.pricing.update").json()
    success = [row for row in logs if row["outcome"] == "success"]
    assert len(success) == 1 and set(success[0]["details"]) == {
        "before_hash",
        "after_hash",
        "revision",
    }


def test_price_and_revision_rollback_with_failed_audit(client, monkeypatch):
    model = entry(client, channel(client))
    path = f"/api/v1/model-entries/{model['id']}/pricing"
    original, record = client.get(path).json(), audit.record

    async def fail(session, action, *args, **kwargs):
        if action == "model.pricing.update" and kwargs.get("outcome", "success") == "success":
            raise RuntimeError("test-only audit failure")
        return await record(session, action, *args, **kwargs)

    monkeypatch.setattr(audit, "record", fail)
    response = client.put(path, json={"expected_revision": 0, "config": config()})
    assert response.status_code == 500
    assert client.get(path).json() == original


@pytest.mark.parametrize("stage", ["waiting", "registered"])
def test_tariff_is_frozen_after_permit_and_before_the_only_send(client, monkeypatch, stage):
    model = entry(client, channel(client))
    price(client, model)
    waiting, release = Event(), Event()
    rt = client.app.state.runtime
    if stage == "waiting":
        original = rt.models.coordination.inference

        @asynccontextmanager
        async def gated(scope):
            waiting.set()
            assert await asyncio.to_thread(release.wait, 10)
            async with original(scope) as permit:
                yield permit

        monkeypatch.setattr(rt.models.coordination, "inference", gated)
    else:
        original_transport = HttpClient.transport

        def transport(self, provider):
            underlying = original_transport(self, provider)

            async def send(request):
                if request.method == "POST":
                    waiting.set()
                    assert await asyncio.to_thread(release.wait, 10)
                return await underlying.handle_async_request(request)

            return httpx2.MockTransport(send)

        monkeypatch.setattr(HttpClient, "transport", transport)
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(invoke, client)
        try:
            assert waiting.wait(10)
            price(client, model, config(token_rule(input_rate="4", output_rate="16")))
        finally:
            release.set()
        assert pending.result(timeout=15).status_code == 200
    row = client.get("/api/v1/model-invocations").json()[0]
    expected = "0.000176" if stage == "waiting" else "0.000088"
    assert Decimal(row["estimate_amount"]) == Decimal(expected)
    assert row["pricing_revision"] == (2 if stage == "waiting" else 1)
    saved = stored(client, row["id"])
    assert saved.estimate_snapshot["revision"] == row["pricing_revision"]
    assert invoke(client).status_code == 200
    rows = client.get("/api/v1/model-invocations").json()
    assert len(rows) == 2 and Decimal(rows[0]["estimate_amount"]) == Decimal("0.000176")


def test_price_failure_does_not_rollback_success_or_trigger_resend(client, monkeypatch):
    from keystone.platform.model_access.services import pricing

    model = entry(client, channel(client))
    price(client, model)
    monkeypatch.setattr(
        pricing, "estimate", lambda *args: (_ for _ in ()).throw(RuntimeError("bad estimate"))
    )
    assert invoke(client).status_code == 200
    rows = client.get("/api/v1/model-invocations").json()
    assert len(rows) == 1 and rows[0]["status"] == "succeeded" and rows[0]["error_code"] is None
    assert rows[0]["estimate_status"] == "invalid" and rows[0]["estimate_amount"] is None
    assert rows[0]["cost_amount"] is not None


def test_inherit_disable_clear_and_target_binding_are_explicit(client):
    model = entry(client, channel(client))
    price(client, model)
    original = client.patch(f"/api/v1/model-entries/{model['id']}/name", json={"name": "Renamed"})
    assert original.status_code == 200
    assert client.get(f"/api/v1/model-entries/{model['id']}/pricing").json()["revision"] == 1
    change = {"channel_id": model["channel_id"], "provider_model": "different"}
    assert client.patch(f"/api/v1/model-entries/{model['id']}", json=change).status_code == 409
    price(client, model, {"mode": "disabled", "rules": []})
    assert invoke(client).status_code == 200
    row = client.get("/api/v1/model-invocations").json()[0]
    assert row["estimate_reason"] == "pricing_disabled"
    price(client, model, {"mode": "inherit", "rules": []})
    assert client.patch(f"/api/v1/model-entries/{model['id']}", json=change).status_code == 200
    assert invoke(client).status_code == 200
    assert client.get("/api/v1/model-invocations").json()[0]["estimate_reason"] == "price_missing"


def test_statistics_currencies_unknown_coverage_and_actuals_stay_separate(client):
    upstream = channel(client)
    cny = entry(client, upstream, key="cny")
    usd = entry(client, upstream, key="usd")
    free = entry(client, upstream, key="free")
    missing = entry(client, upstream, key="missing")
    partial = entry(client, upstream, key="partial")
    price(client, cny)
    price(client, usd, config(token_rule(currency="USD")))
    price(client, free, config(token_rule(input_rate="0", output_rate="0")))
    price(client, partial, config(token_rule(cached=True, writes=True)))
    for model in (cny, usd, free, missing, partial):
        assert invoke(client, key=model["key"]).status_code == 200
    report = client.get("/api/v1/model-invocations/statistics?cost_basis=estimated").json()[
        "summary"
    ]
    assert report["calls"] == 5 and report["known_calls"] == 3 and report["unknown_cost_calls"] == 2
    assert report["estimate_coverage"] == {
        "estimated": 3,
        "pending": 0,
        "partial": 1,
        "unavailable": 1,
        "invalid": 0,
        "snapshot_missing": 0,
        "price_missing": 1,
    }
    assert {r["unit"]: Decimal(r["amount"]) for r in report["costs"]} == {
        "CNY": Decimal("0.000088"),
        "USD": Decimal("0.000088"),
    }
    actual = client.get("/api/v1/model-invocations/statistics").json()["summary"]
    filtered = client.get(
        "/api/v1/model-invocations/statistics?cost_basis=estimated&cost_unit=CNY"
    ).json()["summary"]
    assert (filtered["calls"], filtered["known_calls"], filtered["unknown_cost_calls"]) == (3, 2, 1)
    assert actual["known_calls"] == 5
    assert [(r["unit"], Decimal(r["amount"])) for r in actual["costs"]] == [
        ("credits", Decimal("0.006"))
    ]
    summary = client.get("/api/v1/model-invocations/summary?cost_basis=estimated").json()
    assert summary["unknown_cost_attempts"] == 2 and summary["estimate_statuses"]["estimated"] == 3
    assert (
        client.get("/api/v1/model-invocations?cost_basis=estimated&cost_status=pending").status_code
        == 422
    )
    assert (
        client.get(
            "/api/v1/model-invocations/statistics?cost_basis=estimated&cost_source=response"
        ).status_code
        == 422
    )
    assert (
        client.get(
            "/api/v1/model-invocations/statistics?cost_basis=estimated&group_by=estimate_status"
        ).status_code
        == 200
    )


def test_embedding_batch_is_one_original_request_with_native_input_meter(client, monkeypatch):
    model = entry(
        client,
        channel(client, "openrouter", api_base=CHANNELS["openrouter"].api_base),
        operations=["text.embed"],
    )
    price(client, model, config(operation="text.embed"))
    calls, model_runtime, identifier = mock_embedding(monkeypatch), runtime(client), uuid4()

    async def send():
        selected = await model_runtime.service.selection("chat", operation="text.embed")
        await model_runtime.invoke_embedding(
            EmbeddingRequest(texts=["example"] * 16, dimensions=3),
            selection=selected,
            invocation_id=identifier,
            context=None,
            worker_identity=None,
        )

    client.portal.call(send)
    row = stored(client, identifier)
    assert len(calls) == 1 and row.estimate_amount == Decimal("0.000018")
    assert row.estimate_facts["values"]["events.upstream_success_count"]["value"] == 1
    assert row.estimate_facts["values"]["usage.output_total_tokens"]["value"] == 0


def test_structured_text_uses_the_same_registration_and_meter(client):
    model = entry(client, channel(client), operations=["text.structured"])
    price(client, model, config(operation="text.structured"))
    model_runtime, identifier = runtime(client), uuid4()

    async def send():
        selected = await model_runtime.service.selection("chat", operation="text.structured")
        await model_runtime.invoke_text(
            StructuredRequest(
                messages=[{"role": "user", "content": "private"}],
                output={
                    "name": "answer",
                    "json_schema": {
                        "type": "object",
                        "properties": {"answer": {"type": "integer"}},
                        "required": ["answer"],
                        "additionalProperties": False,
                    },
                },
            ),
            selection=selected,
            invocation_id=identifier,
            context=None,
            worker_identity=None,
        )

    client.portal.call(send)
    row = stored(client, identifier)
    assert row.status == "succeeded" and row.estimate_amount == Decimal("0.000088")
    assert "private" not in json.dumps(row.estimate_facts)


def test_reference_prices_are_exactly_bound_and_never_inherited_by_relays(client):
    native = entry(
        client,
        channel(client, "aliyun_bailian", api_base=ALI_BASE),
        key="native",
        provider_model="qwen-max",
    )
    relay = entry(client, channel(client), key="relay", provider_model="qwen-max")
    assert client.get(f"/api/v1/model-entries/{native['id']}/pricing").json()["operations"][0][
        "reference"
    ]
    assert (
        client.get(f"/api/v1/model-entries/{relay['id']}/pricing").json()["operations"][0][
            "reference"
        ]
        is None
    )
    for key in ("native", "relay"):
        assert invoke(client, key=key).status_code == 200
    rows = {row["model_key"]: row for row in client.get("/api/v1/model-invocations").json()}
    assert Decimal(rows["native"]["estimate_amount"]) == Decimal("0.0001056")
    assert rows["relay"]["estimate_reason"] == "price_missing"


def test_interrupted_sse_preserves_partial_usage_without_a_full_total_or_resend(
    client, monkeypatch
):
    model = entry(client, channel(client))
    price(client, model)
    sends = []

    def transport(self, provider):
        async def respond(request):
            sends.append(request.method)

            class BrokenStream(httpx2.AsyncByteStream):
                async def __aiter__(self):
                    payload = {
                        "id": "partial",
                        "choices": [
                            {"index": 0, "delta": {"content": "part"}, "finish_reason": None}
                        ],
                        "usage": {"prompt_tokens": 12, "completion_tokens": 8},
                    }
                    yield b"data: " + json.dumps(payload).encode() + b"\n\n"
                    raise httpx2.ReadError("test interrupted", request=request)

            return httpx2.Response(
                200, headers={"content-type": "text/event-stream"}, stream=BrokenStream()
            )

        return httpx2.MockTransport(respond)

    monkeypatch.setattr(HttpClient, "transport", transport)
    assert invoke(client, stream=True).status_code == 200
    row = client.get("/api/v1/model-invocations").json()[0]
    assert row["status"] == "unknown" and row["estimate_status"] == "partial"
    assert row["estimate_amount"] is None and len(sends) == 1
    assert (
        stored(client, row["id"]).estimate_facts["values"]["usage.input_total_tokens"]["complete"]
        is False
    )


@pytest.mark.parametrize(
    "receipt,request_price",
    [
        ("early", False),
        ("stale_details", False),
        ("after_final", False),
        ("early", True),
    ],
)
def test_sse_done_does_not_promote_early_or_stale_usage_to_a_final_receipt(
    client, monkeypatch, receipt, request_price
):
    model = entry(client, channel(client))
    rule = (
        image_rule(request=True)
        if request_price
        else token_rule(cached=receipt == "stale_details", writes=receipt == "stale_details")
    )
    price(client, model, config(rule))
    sends = []

    def transport(self, provider):
        async def respond(request):
            sends.append(request.method)

            class Stream(httpx2.AsyncByteStream):
                async def __aiter__(self):
                    chunks = [
                        {
                            "choices": []
                            if receipt == "after_final"
                            else [
                                {
                                    "index": 0,
                                    "delta": {"role": "assistant", "content": "first"},
                                    "finish_reason": None,
                                }
                            ],
                            "usage": {
                                "prompt_tokens": 12,
                                "completion_tokens": 1,
                                "prompt_tokens_details": {
                                    "cached_tokens": 2,
                                    "cache_write_tokens": 1,
                                },
                            },
                        },
                        {
                            "choices": [
                                {"index": 0, "delta": {"content": "last"}, "finish_reason": None}
                            ]
                        },
                        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                    ]
                    if receipt == "stale_details":
                        chunks.append(
                            {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 8}}
                        )
                    for chunk in chunks:
                        yield (
                            b"data: "
                            + json.dumps({"id": "receipt-test", **chunk}).encode()
                            + b"\n\n"
                        )
                    yield b"data: [DONE]\n\n"

            return httpx2.Response(
                200, headers={"content-type": "text/event-stream"}, stream=Stream()
            )

        return httpx2.MockTransport(respond)

    monkeypatch.setattr(HttpClient, "transport", transport)
    assert invoke(client, stream=True).status_code == 200
    row = client.get("/api/v1/model-invocations").json()[0]
    assert row["status"] == "succeeded" and len(sends) == 1
    assert row["estimate_status"] == ("estimated" if request_price else "partial")
    assert (Decimal(row["estimate_amount"]) if row["estimate_amount"] is not None else None) == (
        Decimal("0.2") if request_price else None
    )
    facts = stored(client, row["id"]).estimate_facts["values"]
    assert facts["events.upstream_success_count"]["complete"] is True
    if receipt == "stale_details":
        assert "usage.input_cache_read_tokens" not in facts
        assert "usage.input_cache_write_tokens" not in facts
    else:
        assert facts["usage.input_total_tokens"]["complete"] is False

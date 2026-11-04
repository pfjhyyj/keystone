"""Actual result counts survive parsing/download errors and original-task recovery."""

import base64
import json
from decimal import Decimal

import httpx2
import pytest
from support import model_stub
from support.pricing import config, image_rule, token_rule
from test_files_dictionary import client as client
from test_files_dictionary import upload
from test_identity import query
from test_image_task_service import submit
from test_model_access import channel, entry
from test_model_access import model_configuration as model_configuration
from test_pricing import price, stored

from keystone.core.errors import ServiceError
from keystone.platform.model_access.adapters.http import HttpClient
from keystone.platform.model_access.models import ModelInvocation
from keystone.platform.model_access.schemas.errors import AdapterError

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("provider", ["openrouter", "yunwu", "openlux", "newapi"])
@pytest.mark.parametrize("operation", ["image.generate", "image.edit"])
def test_actual_generated_count_and_effective_sdk_parameters(
    client, monkeypatch, provider, operation
):
    base = "https://openrouter.ai/api/v1" if provider == "openrouter" else "https://yunwu.ai/v1"
    if provider == "openlux":
        base = "https://api.openlux.ai/v1"
    model = entry(
        client,
        channel(client, provider, api_base=base),
        provider_model="gpt-image-2",
        operations=[operation],
    )
    rule = image_rule()
    if provider != "openrouter":
        high = image_rule("0.5")["root"]
        high["id"] = "high"
        rule["root"] = {
            "kind": "branch",
            "when": {"field": "context.quality", "op": "eq", "value": "high"},
            "then": high,
            "else": rule["root"],
        }
    price(client, model, config(rule, operation))
    underlying, bodies = HttpClient.transport, []

    def transport(self, current_provider):
        fallback = underlying(self, current_provider)

        async def respond(request):
            if request.method == "POST" and "/images" in request.url.path:
                bodies.append(request.content)
                # Requested n=2, upstream actually returned one generated result.
                return httpx2.Response(
                    200,
                    json={
                        "id": "test-image",
                        "data": [{"b64_json": base64.b64encode(model_stub.image_bytes()).decode()}],
                    },
                )
            return await fallback.handle_async_request(request)

        return httpx2.MockTransport(respond)

    monkeypatch.setattr(HttpClient, "transport", transport)
    body = {"model_key": "chat", "prompt": "private image prompt", "n": 2}
    if provider != "openrouter":
        body.update(quality="high", aspect_ratio="16:9")
    if operation == "image.edit":
        image = upload(
            client, model_stub.image_bytes(), content_type="image/png", name="ref.png"
        ).json()
        body["images"] = [{"file_id": image["id"]}]
    path = "/api/v1/_test/" + ("generate-image" if operation == "image.generate" else "edit-image")
    response = client.post(path, json=body)
    assert response.status_code == 200, response.text
    row = stored(client, response.json()["invocation_id"])
    assert row.estimate_amount == Decimal("0.2" if provider == "openrouter" else "0.5")
    assert row.estimate_facts["values"]["usage.generated_images"]["value"] == 1
    assert row.estimate_facts["values"]["events.upstream_success_count"]["value"] == 1
    context = row.estimate_snapshot["context"]
    if provider != "openrouter":
        assert context["quality"] == "high" and "x" in context["size"]
        assert context["reference_images"] == (1 if operation == "image.edit" else 0)
    assert len(bodies) == 1
    assert "private image prompt" not in json.dumps(row.estimate_snapshot)


def test_bad_base64_keeps_confirmed_upstream_generation_and_separate_actual_fee(
    client, monkeypatch
):
    model = entry(client, channel(client), operations=["image.generate"])
    price(client, model, config(image_rule(), "image.generate"))
    monkeypatch.setattr(
        HttpClient,
        "transport",
        lambda *args: httpx2.MockTransport(
            lambda request: httpx2.Response(
                200,
                json={
                    "id": "b64-receipt",
                    "data": [{"b64_json": "invalid-base64"}],
                    "usage": {"cost": "0.1"},
                },
            )
        ),
    )
    response = client.post(
        "/api/v1/_test/generate-image", json={"model_key": "chat", "prompt": "test"}
    )
    assert response.status_code == 502
    row = client.get("/api/v1/model-invocations").json()[0]
    assert row["status"] == "unknown" and row["estimate_status"] == "estimated"
    assert Decimal(row["estimate_amount"]) == Decimal("0.2")
    assert Decimal(row["cost_amount"]) == Decimal("0.1")


def test_vision_prices_reported_tokens_without_guessing_picture_token_count(client):
    model = entry(client, channel(client), operations=["image.understand"])
    price(client, model, config(token_rule(), "image.understand"))
    image = upload(
        client, model_stub.image_bytes(), content_type="image/png", name="ref.png"
    ).json()
    response = client.post(
        "/api/v1/_test/understand-image",
        json={
            "model_key": "chat",
            "messages": [{"role": "user", "content": "describe"}],
            "images": [{"file_id": image["id"]}],
        },
    )
    assert response.status_code == 200, response.text
    row = stored(client, response.json()["invocation_id"])
    assert row.estimate_amount == Decimal("0.000088")
    assert "usage.input_image_tokens" not in row.estimate_facts["values"]


def test_kling_download_failure_recovery_and_repeated_terminal_receipts_use_original_price(
    client, monkeypatch
):
    tasks, selected, context, original, requests = submit(
        client,
        monkeypatch,
        condition="invalid_image",
        price_config=config(image_rule(), "image.generate", "async"),
    )
    assert original.estimate_status == "pending"

    async def first_poll():
        with pytest.raises((AdapterError, ServiceError)):
            await tasks.poll(original.id, selected=selected, context=context)

    client.portal.call(first_poll)
    first = stored(client, original.id)
    assert first.status == "succeeded" and first.estimate_status == "estimated"
    assert first.estimate_amount == Decimal("0.2")
    facts = first.estimate_facts
    price(
        client,
        {"id": str(selected.model_entry_id)},
        config(image_rule("9"), "image.generate", "async"),
    )

    async def download(*args):
        return model_stub.image_bytes()

    monkeypatch.setattr(tasks.clients.inference, "download_task_output", download)

    async def allow_query(session):
        row = await session.get(ModelInvocation, original.id)
        row.result_context = {
            **row.result_context,
            "next_query_at": row.result_context["submitted_at"],
        }

    query(client, allow_query)
    client.portal.call(lambda: tasks.poll(original.id, selected=selected, context=context))
    recovered = stored(client, original.id)
    assert recovered.estimate_amount == Decimal("0.2") and recovered.estimate_facts == facts
    assert recovered.estimate_snapshot["revision"] == 1
    assert sum(method == "POST" for method, _, _ in requests) == 1
    assert recovered.result_context["kind"] == "images"


def test_cross_process_image_result_keeps_original_call_estimate(client, monkeypatch, tmp_path):
    from test_flow import wait_run, worker
    from test_model_images import accept, code, invocations

    run_id = accept(client, monkeypatch, n=2)
    model = client.get("/api/v1/model-entries").json()[0]
    price(client, model, config(image_rule(), "image.generate"))
    request_log = tmp_path / "pricing-requests.jsonl"
    with worker(client, tmp_path, code=code(request_log)) as (process, log):
        row = wait_run(client, run_id, process, log)
    assert row["status"] == "succeeded"
    calls = invocations(client)
    assert len(calls) == 1 and calls[0].estimate_amount == Decimal("0.4")
    summary = client.get(f"/api/v1/flow-runs/{run_id}/costs?cost_basis=estimated").json()
    assert summary["cost_basis"] == "estimated" and len(summary["known_costs"]) == 1
    assert Decimal(summary["known_costs"][0]["amount"]) == Decimal("0.4")

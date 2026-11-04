"""Standard NewAPI reconciliation and recovery never repeat generation or invent missing costs."""

from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import httpx2
import pytest
from support import model_stub
from test_files_dictionary import client as client
from test_identity import query
from test_model_access import SECRET, channel, entry, invoke
from test_model_access import model_configuration as model_configuration

from keystone.core.time import utcnow
from keystone.platform.model_access.models import Channel, ModelInvocation
from keystone.platform.model_access.services.costs import CostService

pytestmark = pytest.mark.postgres


def test_newapi_balance_persists_account_remaining_separately_from_unlimited_key(client):
    configured = channel(
        client, "newapi", api_base="https://relay.example/gateway/v1", billing_api_key=None
    )
    response = client.post(f"/api/v1/model-channels/{configured['id']}/balance")
    assert response.status_code == 200, response.text
    snapshot = response.json()["balance_snapshot"]
    assert snapshot == {
        "scope": "provider_account",
        "unlimited": True,
        "items": [
            {"kind": "remaining", "amount": "192.851716", "unit": "USD"},
            {"kind": "used", "amount": "64.453848", "unit": "USD"},
        ],
        "token_items": [{"kind": "used", "amount": "0.000084", "unit": "USD"}],
    }
    saved = client.get("/api/v1/model-channels").json()[0]
    assert saved["balance_snapshot"] == snapshot and saved["balance_check_status"] == "ok"


@pytest.fixture
def billing(client, monkeypatch):
    state = {"records": [], "posts": 0, "scale": 500000, "format": "list", "status": 200}

    async def respond(request):
        if request.method == "POST":
            state["posts"] += 1
            response = await model_stub.transport("newapi").handle_async_request(request)
            identifier = "gateway-" + request.headers["x-keystone-invocation-id"]
            response.headers["x-oneapi-request-id"] = identifier
            state["records"].append(
                {
                    "request_id": identifier,
                    "type": 2,
                    "model_name": "mock-chat",
                    "quota": 28,
                    "created_at": int(utcnow().timestamp()),
                    "content": "private-upstream-log",
                }
            )
            return response
        if request.url.path == "/api/status":
            return httpx2.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "quota_per_unit": state["scale"],
                        "quota_display_type": "USD",
                    },
                },
            )
        assert request.url.path == "/api/log/token"
        assert request.headers["authorization"] == "Bearer " + SECRET
        assert "key" not in request.url.params
        if state["status"] != 200:
            return httpx2.Response(state["status"])
        data = state["records"]
        if state["format"] == "paged":
            page, size = int(request.url.params["p"]), int(request.url.params["page_size"])
            data = {
                "items": data[(page - 1) * size : page * size],
                "total": len(data),
                "page": page,
                "page_size": size,
            }
        return httpx2.Response(200, json={"success": True, "data": data})

    monkeypatch.setattr(
        client.app.state.runtime.models.http, "transport", lambda _: httpx2.MockTransport(respond)
    )
    configured = channel(client, "newapi", api_base="https://relay.example/v1")
    entry(client, configured)
    state["channel_id"] = UUID(configured["id"])
    return state


def called(client):
    response = invoke(client)
    assert response.status_code == 200, response.text
    return UUID(response.json()["invocation_id"])


def stored(client, identifier):
    return query(client, lambda s: s.get(ModelInvocation, identifier))


@pytest.mark.parametrize("shape", ["list", "paged"])
def test_automatic_and_manual_queries_confirm_actual_cost_without_resending(client, billing, shape):
    billing["format"] = shape
    identifier = called(client)

    async def ready(session):
        row = await session.get(ModelInvocation, identifier)
        row.cost_next_check_at = utcnow() - timedelta(seconds=1)

    query(client, ready)
    runtime = client.app.state.runtime
    client.portal.call(
        CostService(runtime.database, runtime.models).reconcile,
        stored(client, identifier).cost_context["scope"],
    )
    first = stored(client, identifier)
    assert first.status == "succeeded" and first.cost_check_status == "confirmed"
    assert first.cost_amount == Decimal("0.000056") and first.cost_unit == "USD"
    assert first.cost_source == "query" and first.cost_evidence["quota"] == 28
    assert first.cost_evidence["quota_per_unit"] == 500000
    assert first.upstream_request_id == billing["records"][0]["request_id"]
    assert "private-upstream-log" not in str(first.cost_evidence)
    assert client.post(f"/api/v1/model-invocations/{identifier}/cost").status_code == 200
    assert stored(client, identifier).cost_amount == first.cost_amount
    assert billing["posts"] == 1


def test_recent_log_cap_reports_older_missing_call_as_unavailable(client, billing):
    identifier = called(client)
    now = int(utcnow().timestamp())
    billing["records"] = [
        {
            "request_id": f"unrelated-{n}",
            "type": 2,
            "model_name": "mock-chat",
            "quota": 1,
            "created_at": now + n,
        }
        for n in range(1000)
    ]

    async def older(session):
        row = await session.get(ModelInvocation, identifier)
        row.created_at = utcnow() - timedelta(hours=2)

    query(client, older)
    assert client.post(f"/api/v1/model-invocations/{identifier}/cost").status_code == 200
    current = stored(client, identifier)
    assert current.cost_check_status == "unavailable"
    assert (
        current.cost_check_reason == "cost_log_window_unavailable" and current.cost_amount is None
    )


def test_conflicting_exact_receipts_are_not_charged_twice(client, billing):
    identifier = called(client)
    billing["records"].append({**billing["records"][0], "quota": 29})
    assert client.post(f"/api/v1/model-invocations/{identifier}/cost").status_code == 200
    current = stored(client, identifier)
    assert (
        current.cost_check_status == "blocked"
        and current.cost_check_reason == "cost_receipt_conflict"
    )
    assert current.cost_amount is None and billing["posts"] == 1


def test_auth_failure_keeps_cost_unknown_and_original_call(client, billing):
    identifier = called(client)
    billing["status"] = 401
    assert client.post(f"/api/v1/model-invocations/{identifier}/cost").status_code == 200
    current = stored(client, identifier)
    assert (
        current.cost_check_status == "blocked" and current.cost_check_reason == "cost_auth_required"
    )
    assert current.cost_amount is None and current.status == "succeeded" and billing["posts"] == 1


def test_standard_and_yunwu_compatible_profiles_do_not_hijack_each_others_scope(client, billing):
    original = query(client, lambda s: s.get(Channel, billing["channel_id"]))
    other = channel(client, "yunwu", api_base=original.api_base)
    second = query(client, lambda s: s.get(Channel, UUID(other["id"])))
    assert original.cost_scope != second.cost_scope
    assert (
        original.cost_context["provider"] == "newapi" and second.cost_context["provider"] == "yunwu"
    )


def test_api_base_paths_can_both_reconcile_without_context_conflicts(client, billing):
    second = channel(client, "newapi", api_base="https://relay.example")
    entry(client, second, key="other")
    first_id = called(client)
    response = invoke(client, key="other")
    assert response.status_code == 200, response.text
    second_id = UUID(response.json()["invocation_id"])
    for identifier in (first_id, second_id):
        response = client.post(f"/api/v1/model-invocations/{identifier}/cost")
        assert response.status_code == 200, response.text
        assert response.json()["cost_check_status"] == "confirmed"
        assert Decimal(response.json()["cost_amount"]) == Decimal("0.000056")
    assert billing["posts"] == 2


def test_missing_cost_context_is_not_rebound(client, billing):
    identifier = called(client)

    async def missing(session):
        row = await session.get(ModelInvocation, identifier)
        row.cost_context = None
        row.cost_check_status, row.cost_check_reason = "unavailable", "cost_query_unavailable"

    query(client, missing)
    assert client.post(f"/api/v1/model-invocations/{identifier}/cost").status_code == 200
    assert stored(client, identifier).cost_context is None
    assert stored(client, identifier).cost_amount is None
    assert billing["posts"] == 1

from decimal import Decimal

import httpx2
import pytest

from keystone.core.errors import ServiceError
from keystone.platform.model_access.adapters.billing.contracts import BillingQueryError
from keystone.platform.model_access.adapters.billing.newapi import Newapi
from keystone.platform.model_access.adapters.billing.registry import BillingClient
from keystone.platform.model_access.adapters.billing.token_logs import quota_scale
from keystone.platform.model_access.adapters.billing.yunwu import Yunwu
from keystone.platform.model_access.adapters.http import HttpClient


def entry(**changes):
    return {
        "request_id": "gateway-request",
        "type": 2,
        "model_name": "deepseek-flash",
        "quota": 28,
        "created_at": 100,
        "content": "private-prompt-never-save",
        "other": "not-needed-when-top-level-id-exists",
        **changes,
    }


@pytest.mark.parametrize(
    "paginated,scale",
    [
        (False, 500000),
        (True, 500000),
        (False, 10000),
    ],
)
async def test_newapi_bearer_logs_use_actual_scale_and_preserve_only_cost_evidence(
    paginated, scale
):
    adapter = Newapi("https://relay.example/gateway/v1")
    context = adapter.binding(adapter.api_base).snapshot("scope", "newapi", adapter.api_base)
    calls = []

    def respond(request):
        calls.append(request)
        assert request.url.host == "relay.example"
        assert "key" not in request.url.params
        if request.url.path.endswith("/api/status"):
            assert "authorization" not in request.headers
            return httpx2.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "quota_per_unit": scale,
                        "quota_display_type": "CNY",
                        "usd_exchange_rate": 7.2,
                    },
                },
            )
        assert request.url.path == "/gateway/api/log/token"
        assert request.headers["authorization"] == "Bearer private-key"
        data = (
            {"items": [entry()], "total": 1, "page": 1, "page_size": 20} if paginated else [entry()]
        )
        return httpx2.Response(200, json={"success": True, "data": data})

    http = HttpClient()
    http.transport = lambda _: httpx2.MockTransport(respond)
    policy = await adapter.validate_policy(http, context, "private-key")
    page = await adapter.page(
        http, {**context, **policy}, "private-key", start=0, end=200, page=1, page_size=20
    )
    assert page.complete and len(calls) == 2
    assert page.records == [
        {
            "request_id": "gateway-request",
            "amount": format(Decimal(28) / Decimal(scale), "f"),
            "unit": "USD",
            "quota": 28,
            "quota_per_unit": scale,
            "model": "deepseek-flash",
        }
    ]
    assert "private" not in str(page.records)


def test_gateway_receipt_precedes_provider_body_id_and_other_trace_ids():
    assert (
        Newapi()
        .receipt(
            {
                "body_id": "provider-id",
                "x-request-id": "trace",
                "x-api-request-id": "alternate",
                "x-oneapi-request-id": "gateway-request",
            },
            None,
        )
        .request_id
        == "gateway-request"
    )
    assert Newapi().receipt({"body_id": "provider-id"}, None).request_id is None


async def test_recent_log_cap_retains_coverage_boundary_without_pretending_to_paginate():
    adapter = Newapi("https://relay.example/v1")
    context = {
        **adapter.binding(adapter.api_base).snapshot("scope", "newapi", adapter.api_base),
        "quota_per_unit": 500000,
    }
    data = [entry(request_id=f"request-{n}", created_at=100 + n) for n in range(1000)]
    http = HttpClient()
    http.transport = lambda _: httpx2.MockTransport(
        lambda _: httpx2.Response(200, json={"success": True, "data": data})
    )
    result = await adapter.page(http, context, "key", start=0, end=2000, page=1, page_size=20)
    assert result.complete and result.covered_since == 100 and len(result.records) == 1000


@pytest.mark.parametrize("value", [None, True, 0, -1, "500000", "private-upstream-message"])
async def test_bad_quota_policy_does_not_guess_a_price(value):
    adapter = Newapi()
    context = adapter.binding(adapter.api_base).snapshot("scope", "newapi", adapter.api_base)
    http = HttpClient()
    http.transport = lambda _: httpx2.MockTransport(
        lambda _: httpx2.Response(200, json={"success": True, "data": {"quota_per_unit": value}})
    )
    with pytest.raises(BillingQueryError) as error:
        await adapter.validate_policy(http, context, "private-key")
    assert error.value.reason == "cost_policy_changed"


@pytest.mark.parametrize("unlimited", [False, True])
async def test_token_balance_is_not_account_balance_and_unlimited_has_no_zero_remaining(unlimited):
    def respond(request):
        if request.url.path.endswith("/api/status"):
            return httpx2.Response(200, json={"success": True, "data": {"quota_per_unit": 1000}})
        assert request.url.path == "/gateway/api/usage/token/"
        assert request.headers["authorization"] == "Bearer private-key"
        assert "key" not in request.url.params
        return httpx2.Response(
            200,
            json={
                "code": True,
                "data": {
                    "object": "token_usage",
                    "name": "private-account-name",
                    "unlimited_quota": unlimited,
                    "total_used": 100,
                    "total_available": 0 if unlimited else 500,
                    "total_granted": 600,
                },
            },
        )

    http = HttpClient()
    http.transport = lambda _: httpx2.MockTransport(respond)
    result = await BillingClient(http).balance(
        "newapi", "https://relay.example/gateway/v1", "private-key"
    )
    assert result["scope"] == "token_quota" and result["unlimited"] is unlimited
    assert "private" not in str(result)
    assert {item["kind"] for item in result["items"]} == (
        {"used"} if unlimited else {"remaining", "used", "granted"}
    )
    assert next(item["amount"] for item in result["items"] if item["kind"] == "used") == "0.1"


@pytest.mark.parametrize(
    "unlimited,remaining",
    [
        (False, 96425858),
        (True, 96425858),
        (False, 0),
        (False, -100),
    ],
)
async def test_account_remaining_is_independent_of_token_limit(unlimited, remaining):
    def respond(request):
        if request.url.path == "/api/status":
            return httpx2.Response(200, json={"success": True, "data": {"quota_per_unit": 500000}})
        return httpx2.Response(
            200,
            json={
                "code": True,
                "data": {
                    "name": "private-token-name",
                    "unlimited_quota": unlimited,
                    "total_available": 0 if unlimited else 500,
                    "total_used": 100,
                    "total_granted": 600,
                    "user_quota": {
                        "total_available": remaining,
                        "total_used": 32226924,
                        "private-account-data": "do-not-save",
                    },
                },
            },
        )

    http = HttpClient()
    http.transport = lambda _: httpx2.MockTransport(respond)
    result = await BillingClient(http).balance("newapi", Newapi.api_base, "private-key")
    assert result == {
        "scope": "provider_account",
        "unlimited": unlimited,
        "items": [
            {"kind": "remaining", "amount": str(Decimal(remaining) / 500000), "unit": "USD"},
            {"kind": "used", "amount": "64.453848", "unit": "USD"},
        ],
        "token_items": (
            [{"kind": "used", "amount": "0.0002", "unit": "USD"}]
            if unlimited
            else [
                {"kind": "remaining", "amount": "0.001", "unit": "USD"},
                {"kind": "used", "amount": "0.0002", "unit": "USD"},
                {"kind": "granted", "amount": "0.0012", "unit": "USD"},
            ]
        ),
    }


@pytest.mark.parametrize(
    "account",
    [
        None,
        [],
        {},
        {"total_available": True, "total_used": 0},
        {"total_available": "private-value", "total_used": 0},
        {"total_available": 1, "total_used": -1},
        {"total_available": 2**63, "total_used": 0},
    ],
)
async def test_invalid_account_quota_is_not_reported_as_a_successful_token_only_balance(account):
    def respond(request):
        data = (
            {"quota_per_unit": 500000}
            if request.url.path == "/api/status"
            else {"unlimited_quota": True, "total_used": 1, "user_quota": account}
        )
        return httpx2.Response(200, json={"success": True, "data": data})

    http = HttpClient()
    http.transport = lambda _: httpx2.MockTransport(respond)
    with pytest.raises(ServiceError) as error:
        await BillingClient(http).balance("newapi", Newapi.api_base, "private-key")
    assert error.value.code == "model_query_failed" and "private" not in str(error.value)


async def test_yunwu_compatible_profile_keeps_query_key_pagination_and_credit_units():
    adapter = Yunwu("https://compatible.example/gateway/v1")
    context = adapter.binding(adapter.api_base).snapshot("scope", "yunwu", adapter.api_base)

    def respond(request):
        if request.url.path.endswith("/api/status"):
            return httpx2.Response(200, json={"success": True, "data": {"quota_per_unit": 1000}})
        assert request.url.path == "/gateway/api/log/token"
        assert request.url.params["key"] == "private-key" and "authorization" not in request.headers
        item = entry(request_id=None, other='{"request_id":"gateway-request"}')
        return httpx2.Response(
            200,
            json={
                "success": True,
                "data": {
                    "items": [item],
                    "page": 1,
                    "page_size": 20,
                    "total": 1,
                },
            },
        )

    http = HttpClient()
    http.transport = lambda _: httpx2.MockTransport(respond)
    policy = await adapter.validate_policy(http, context, "private-key")
    result = await adapter.page(
        http, {**context, **policy}, "private-key", start=0, end=200, page=1, page_size=20
    )
    assert result.records[0]["unit"] == "yunwu_credits" and result.records[0]["amount"] == "0.028"


def test_nonfinite_quota_policy_is_a_normal_invalid_value():
    with pytest.raises(ValueError):
        quota_scale({"success": True, "data": {"quota_per_unit": Decimal("NaN")}})


@pytest.mark.parametrize("adapter_type", [Newapi, Yunwu])
def test_different_api_bases_with_same_billing_origin_have_distinct_scopes(adapter_type):
    bindings = [
        adapter_type(base).binding(base)
        for base in ("https://relay.example/v1", "https://relay.example")
    ]
    assert bindings[0].origin == bindings[1].origin
    assert (bindings[0].scope_origin or bindings[0].origin) != (
        bindings[1].scope_origin or bindings[1].origin
    )

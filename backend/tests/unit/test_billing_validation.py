"""Billing response validation uses real HTTP parsing without application or SQL setup."""

import json

import httpx2
import pytest

from keystone.platform.model_access.adapters.billing.contracts import BillingQueryError
from keystone.platform.model_access.adapters.billing.openlux import Openlux
from keystone.platform.model_access.adapters.http import HttpClient

SECRET = "private-billing-token"


async def test_safe_query_failure_classification(monkeypatch):
    adapter = Openlux()
    context = adapter.binding(adapter.api_base).snapshot("test-scope", "openlux", adapter.api_base)
    for reason in ("network", "quota", "policy", "envelope", "depth", "model"):
        record = {
            "type": 2,
            "quota": 1328,
            "model_name": "mock-chat",
            "other": json.dumps({"request_id": "bill-one"}),
        }
        policy = {"quota_per_unit": 500000, "currency_type": "USD"}
        if reason == "quota":
            record["quota"] = -1
        elif reason == "policy":
            policy["currency_type"] = "CNY"
        elif reason == "envelope":
            record["other"] = "not-json"
        elif reason == "depth":
            record["other"] = "[" * 1100 + "0" + "]" * 1100
        elif reason == "model":
            record["model_name"] = "m" * 129
        calls = []

        def respond(request, *, calls=calls, policy=policy, reason=reason, record=record):
            calls.append(request.url.path)
            assert request.method == "GET"
            if request.url.path == "/api/status":
                assert "key" not in request.url.params
                return httpx2.Response(200, json={"success": True, "data": policy})
            assert request.url.path == "/api/log/token"
            assert request.url.params["key"] == SECRET
            if reason == "network":
                raise httpx2.ConnectError(str(request.url))
            return httpx2.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "items": [record],
                        "total": 1,
                        "page": 1,
                        "page_size": 20,
                    },
                },
            )

        http = HttpClient()
        monkeypatch.setattr(
            http, "transport", lambda _, respond=respond: httpx2.MockTransport(respond)
        )
        with pytest.raises(BillingQueryError) as error:
            await adapter.validate_policy(http, context, SECRET)
            await adapter.page(http, context, SECRET, start=0, end=1, page=1, page_size=20)
        expected = {
            "network": "cost_transport_failed",
            "policy": "cost_policy_changed",
        }.get(reason, "cost_invalid_response")
        assert error.value.reason == expected, reason
        assert error.value.blocked is (reason != "network"), reason
        assert SECRET not in str(error.value) and "key=" not in str(error.value)
        assert calls == (
            ["/api/status"] if reason == "policy" else ["/api/status", "/api/log/token"]
        )

"""Cloud credentials, management authorization and external accounting query windows."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from uuid import UUID

import httpx2
import pytest
from test_files_dictionary import client as client
from test_identity import bearer, query, token

from keystone.core.time import utcnow
from keystone.platform.model_access.adapters.channels import CHANNELS
from keystone.platform.model_access.models import Channel

pytestmark = pytest.mark.postgres
KEYS = {
    "access_key_id": "cloud-account-id",
    "access_key_secret": "cloud-account-secret",
    "security_token": "cloud-sts-token",
}


def cloud_channel(client, provider="aliyun_bailian", **changes):
    response = client.post(
        "/api/v1/model-channels",
        json={
            "name": "cloud",
            "provider": provider,
            "api_base": CHANNELS[provider].api_base,
            "api_key": "private-inference-key",
            "billing_credentials": KEYS,
            **changes,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def change(client, row, **values):
    return client.patch(
        f"/api/v1/model-channels/{row['id']}",
        json={
            "name": row["name"],
            "api_base": row["api_base"],
            **values,
        },
    )


def install(client, monkeypatch, handler):
    monkeypatch.setattr(
        client.app.state.runtime.models.http, "transport", lambda _: httpx2.MockTransport(handler)
    )


def period():
    return (utcnow() + timedelta(hours=8)).strftime("%Y-%m")


def bill_payload(provider):
    if provider == "aliyun_bailian":
        return {
            "Success": True,
            "Data": {
                "BillingCycle": period(),
                "Items": {
                    "Item": [
                        {
                            "ProductCode": "sfm",
                            "ProductName": "百炼",
                            "Currency": "CNY",
                            "Item": "PayAsYouGoBill",
                            "PretaxAmount": "1.23456789",
                            "CashAmount": "1.00",
                        }
                    ]
                },
            },
        }
    return {
        "ResponseMetadata": {},
        "Result": {
            "Total": 1,
            "Offset": 0,
            "List": [
                {
                    "Product": "ark",
                    "ProductZh": "火山方舟",
                    "Currency": "CNY",
                    "BillPeriod": period(),
                    "PayableAmount": "1.23456789",
                    "PaidAmount": "1.00",
                }
            ],
        },
    }


@pytest.mark.parametrize("provider", ["aliyun_bailian", "volcengine"])
def test_cloud_credentials_are_encrypted_write_only_and_clearable(client, monkeypatch, provider):
    row = cloud_channel(client, provider)
    encrypted = query(client, lambda session: session.get(Channel, UUID(row["id"])))
    assert all(secret not in encrypted.billing_api_key_encrypted for secret in KEYS.values())
    store = client.app.state.runtime.models.credentials
    raw = store.decrypt(encrypted.billing_api_key_encrypted, encrypted.id, "billing")
    assert json.loads(raw) == KEYS
    assert "billing_credentials" not in row and row["has_billing_api_key"]
    for secret in KEYS.values():
        assert secret not in client.get("/api/v1/model-channels").text
        assert secret not in client.get("/api/v1/audit-logs").text
    payload = (
        {
            "Success": True,
            "Data": {"Currency": "CNY", "AvailableAmount": "3.01", "AvailableCashAmount": "2.01"},
        }
        if provider == "aliyun_bailian"
        else {
            "ResponseMetadata": {},
            "Result": {
                "AvailableBalance": "3.01",
                "CashBalance": "2.01",
                "ArrearsBalance": "0",
                "CreditLimit": "1",
                "FreezeAmount": "0",
            },
        }
    )
    install(client, monkeypatch, lambda _: httpx2.Response(200, json=payload))
    refreshed = client.post(f"/api/v1/model-channels/{row['id']}/balance").json()
    assert refreshed["balance_check_status"] == "ok"
    assert refreshed["balance_snapshot"]["items"][0]["amount"] == "3.01"
    preserved = change(client, row).json()
    assert preserved["has_billing_api_key"]
    assert preserved["balance_snapshot"] == refreshed["balance_snapshot"]
    same = query(client, lambda session: session.get(Channel, UUID(row["id"])))
    assert same.billing_api_key_encrypted == encrypted.billing_api_key_encrypted
    install(client, monkeypatch, lambda _: httpx2.Response(403, text="private cloud error"))
    failed = client.post(f"/api/v1/model-channels/{row['id']}/balance").json()
    assert failed["balance_check_status"] == "error"
    assert failed["balance_snapshot"] == refreshed["balance_snapshot"]
    assert "private cloud error" not in json.dumps(failed)
    rotated = change(
        client, row, billing_credentials={**KEYS, "access_key_secret": "rotated"}
    ).json()
    assert rotated["balance_snapshot"] is None and rotated["balance_check_status"] == "never"
    cleared = change(client, row, clear_billing_api_key=True).json()
    assert not cleared["has_billing_api_key"]
    response = client.get(f"/api/v1/model-channels/{row['id']}/bills", params={"period": period()})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "model_billing_credentials_required"


@pytest.mark.parametrize("provider", ["aliyun_bailian", "volcengine"])
def test_bill_query_requires_management_and_never_changes_invocation_costs(
    client, monkeypatch, provider
):
    row = cloud_channel(client, provider)
    sent = []

    def respond(request):
        sent.append(request)
        return httpx2.Response(200, json=bill_payload(provider))

    install(client, monkeypatch, respond)
    path = f"/api/v1/model-channels/{row['id']}/bills"
    restricted = bearer(token(client, ["model:invoke"])["token"])
    assert client.get(path, params={"period": period()}, headers=restricted).status_code == 403
    assert not sent
    response = client.get(path, params={"period": period()})
    assert response.status_code == 200, response.text
    assert response.json()["scope"] == "cloud_account"
    assert response.json()["items"][0]["amounts"]["应付金额"] == "1.23456789"
    assert "cloud-account-secret" not in response.text and len(sent) == 1
    assert client.get("/api/v1/model-invocations").json() == []
    for invalid in (
        {"period": "2026-13"},
        {"period": "2000-01"},
        {"period": "2099-01"},
        {"period": period(), "offset": -1},
    ):
        assert client.get(path, params=invalid).status_code == 422
    assert len(sent) == 1


@pytest.mark.parametrize("race", ["credentials", "revoke"])
def test_bill_query_discards_results_after_config_change_or_revocation(client, monkeypatch, race):
    row = cloud_channel(client)
    credential = token(client, ["model:manage"])
    entered, release = Event(), Event()

    async def respond(request):
        entered.set()
        assert await asyncio.to_thread(release.wait, 10)
        return httpx2.Response(200, json=bill_payload("aliyun_bailian"))

    install(client, monkeypatch, respond)
    path = f"/api/v1/model-channels/{row['id']}/bills"
    with ThreadPoolExecutor() as executor:
        pending = executor.submit(
            client.get, path, params={"period": period()}, headers=bearer(credential["token"])
        )
        try:
            assert entered.wait(5)
            if race == "credentials":
                assert (
                    change(
                        client, row, billing_credentials={**KEYS, "access_key_secret": "rotated"}
                    ).status_code
                    == 200
                )
            else:
                assert client.delete(f"/api/v1/auth/tokens/{credential['id']}").status_code == 204
        finally:
            release.set()
        response = pending.result(timeout=10)
    assert response.status_code == (409 if race == "credentials" else 401)
    assert "1.23456789" not in response.text


def test_cloud_credentials_reject_other_providers_and_ambiguous_clear(client):
    for provider, changes in [
        ("openrouter", {}),
        ("volcengine", {"clear_billing_api_key": True}),
        ("volcengine", {"billing_api_key": "other"}),
    ]:
        response = client.post(
            "/api/v1/model-channels",
            json={
                "name": "bad",
                "provider": provider,
                "api_base": CHANNELS[provider].api_base,
                "api_key": "inference",
                "billing_credentials": KEYS,
                **changes,
            },
        )
        assert response.status_code == 422
        assert "cloud-account-secret" not in response.text

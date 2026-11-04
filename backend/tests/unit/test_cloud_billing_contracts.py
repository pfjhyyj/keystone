"""Official signature vectors and bounded accounting contracts; no cloud connections."""

import json
from copy import deepcopy

import httpx2
import pytest

from keystone.core.errors import ServiceError
from keystone.platform.model_access.adapters.billing.cloud import (
    aliyun_signature,
    volcengine_headers,
)
from keystone.platform.model_access.adapters.billing.registry import BillingClient
from keystone.platform.model_access.adapters.channels import CHANNELS
from keystone.platform.model_access.adapters.http import HttpClient
from keystone.platform.model_access.schemas.channels import ChannelCreate
from keystone.platform.model_access.schemas.cloud_billing import CloudBillingCredentials


def keys(**overrides):
    return CloudBillingCredentials(
        **{"access_key_id": "test-id", "access_key_secret": "test-secret", **overrides}
    )


def test_aliyun_rpc_signature_matches_official_describe_regions_vector():
    params = {
        "AccessKeyId": "testid",
        "Action": "DescribeRegions",
        "Format": "XML",
        "SignatureMethod": "HMAC-SHA1",
        "SignatureVersion": "1.0",
        "SignatureNonce": "3ee8c1b8-83d3-44af-a94f-4e0ad82fd6cf",
        "Timestamp": "2016-02-23T12:46:24Z",
        "Version": "2014-05-26",
    }
    assert aliyun_signature(params, "testsecret") == "OLeaidS1JvxuMvnyHOwuJ+uX5qY="


@pytest.mark.parametrize(
    "method,action,body,expected",
    [
        (
            "GET",
            "QueryBalanceAcct",
            b"",
            "1eda9e7e6b1728151a8e8791fdaf67cfbd28bd5c80d0fce2eb208746cf483105",
        ),
        (
            "POST",
            "ListBill",
            b'{"Limit":10,"BillPeriod":"2023-08"}',
            "5e8480ceea12d0000a23c054151c50dd02c1a7dec835004057d19f13d53a7658",
        ),
    ],
)
def test_volcengine_signature_matches_official_get_and_post_vectors(method, action, body, expected):
    credential = keys(
        access_key_id="AKLTYWViMTVmZGYzM2E0NDI5Mzk2MDZjNjFmMjc2MjRjMzg",
        access_key_secret="WkRZeE1EQmxPVGhsWWpWak5HVmtNbUUxTXpZeU9UVXlOMlE1TmpZeVlqTQ==",
    )
    headers = volcengine_headers(
        credential,
        "billing.volcengineapi.com",
        {"Action": action, "Version": "2022-01-01"},
        timestamp="20250329T180937Z",
        body=body,
        method=method,
    )
    assert headers["Authorization"].endswith("Signature=" + expected)
    assert "test-secret" not in str(headers)


@pytest.mark.parametrize("value", ["bad\nkey", "has space", "中文", "\x7f", ""])
def test_cloud_credentials_reject_header_injection_and_hide_input(value):
    with pytest.raises(ValueError) as error:
        keys(access_key_secret=value)
    assert value not in str(error.value) or not value
    assert "test-secret" not in repr(keys())


def test_cloud_credential_input_preserves_legacy_keys_and_rejects_ambiguous_updates():
    base = dict(
        name="cloud",
        provider="volcengine",
        api_base=CHANNELS["volcengine"].api_base,
        api_key="inference",
    )
    data = ChannelCreate(**base, billing_credentials=keys())
    assert "test-secret" not in repr(data)
    assert (
        json.loads(data.billing_credentials.encoded().get_secret_value())["access_key_id"]
        == "test-id"
    )
    for changes in [
        dict(billing_api_key="legacy", billing_credentials=keys()),
        dict(clear_billing_api_key=True, billing_credentials=keys()),
    ]:
        with pytest.raises(ValueError) as error:
            ChannelCreate(**base, **changes)
        assert "test-secret" not in str(error.value)
    with pytest.raises(ValueError) as error:
        ChannelCreate(
            **base,
            clear_billing_api_key=True,
            billing_credentials={
                "access_key_id": "private-id",
                "access_key_secret": "private-secret",
            },
        )
    assert "private-secret" not in str(error.value)


@pytest.mark.parametrize("provider", ["aliyun_bailian", "volcengine"])
async def test_signed_balance_requests_use_only_cloud_endpoints_and_keep_account_scope(provider):
    calls = []

    def respond(request):
        calls.append(request)
        assert request.method == "GET" and request.url.path == "/"
        if provider == "aliyun_bailian":
            assert request.url.host == "business.aliyuncs.com"
            assert request.url.params["Action"] == "QueryAccountBalance"
            assert request.url.params["SecurityToken"] == "test-token"
            assert request.url.params["AccessKeyId"] == "test-id"
            assert "Authorization" not in request.headers
            return httpx2.Response(
                200,
                json={
                    "Success": True,
                    "Data": {
                        "Currency": "JPY",
                        "AvailableAmount": "-10.12",
                        "AvailableCashAmount": "5.0",
                    },
                },
            )
        assert request.url.host == "billing.volcengineapi.com"
        assert dict(request.url.params) == {"Action": "QueryBalanceAcct", "Version": "2022-01-01"}
        assert "SignedHeaders=host;x-date;x-security-token" in request.headers["authorization"]
        assert request.headers["x-security-token"] == "test-token"
        assert "test-secret" not in str(request.url)
        return httpx2.Response(
            200,
            json={
                "ResponseMetadata": {},
                "Result": {
                    "AvailableBalance": "-10.12",
                    "CashBalance": "5.0",
                    "CreditLimit": "0.00",
                    "ArrearsBalance": "1.0",
                    "FreezeAmount": "0.10",
                },
            },
        )

    http = HttpClient()
    http.transport = lambda _: httpx2.MockTransport(respond)
    billing = BillingClient(http)
    secret = keys(security_token="test-token").encoded().get_secret_value()
    result = await billing.balance(provider, CHANNELS[provider].api_base, secret)
    assert result["scope"] == (
        "aliyun_account" if provider == "aliyun_bailian" else "volcengine_account"
    )
    assert result["items"][0]["amount"] == "-10.12" and len(calls) == 1
    assert billing.adapter(provider).binding(CHANNELS[provider].api_base) is None
    assert billing.adapter(provider).receipt({"body_id": "call-id"}, "10").fee is None
    with pytest.raises(ServiceError) as error:
        await billing.balance(provider, "https://evil.invalid", secret)
    assert error.value.code == "model_target_not_allowed" and len(calls) == 1
    with pytest.raises(ServiceError) as error:
        await billing.balance(provider, CHANNELS[provider].api_base, "inference-key")
    assert error.value.code == "model_billing_credentials_required" and len(calls) == 1


@pytest.mark.parametrize(
    "provider,status",
    [
        ("aliyun_bailian", 302),
        ("aliyun_bailian", 401),
        ("aliyun_bailian", 403),
        ("aliyun_bailian", 429),
        ("aliyun_bailian", 500),
        ("volcengine", 401),
    ],
)
async def test_accounting_errors_do_not_redirect_retry_or_expose_upstream_body(provider, status):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx2.Response(
            status, text="secret upstream body", headers={"location": "https://evil.invalid"}
        )

    http = HttpClient()
    http.transport = lambda _: httpx2.MockTransport(respond)
    with pytest.raises(ServiceError) as error:
        await BillingClient(http).balance(
            provider, CHANNELS[provider].api_base, keys().encoded().get_secret_value()
        )
    assert len(requests) == 1 and "secret" not in error.value.detail


@pytest.mark.parametrize("provider", ["aliyun_bailian", "volcengine"])
async def test_monthly_bills_keep_reported_decimal_amounts_and_bounded_product_pages(provider):
    def respond(request):
        if provider == "aliyun_bailian":
            assert request.method == "GET" and request.url.params["BillingCycle"] == "2026-09"
            assert request.url.params["Action"] == "QueryBillOverview"
            item = {
                "Currency": "CNY",
                "ProductName": "百炼",
                "ProductCode": "sfm",
                "Item": "Refund",
                "PretaxAmount": "-0.123456789012345678",
                "CashAmount": "-0.1",
                "secret": "never return",
            }
            return httpx2.Response(
                200,
                json={
                    "Success": True,
                    "Data": {"BillingCycle": "2026-09", "Items": {"Item": [item] * 51}},
                },
            )
        assert request.method == "POST" and request.url.params["Action"] == "ListBillDetail"
        body = json.loads(request.content)
        assert body == {
            "BillPeriod": "2026-09",
            "GroupTerm": 2,
            "GroupPeriod": 0,
            "Limit": 50,
            "Offset": 50,
            "NeedRecordNum": 1,
            "IgnoreZero": 0,
        }
        assert request.headers["content-type"] == "application/json"
        item = {
            "BillPeriod": "2026-09",
            "Currency": "CNY",
            "ProductZh": "火山方舟",
            "Product": "ark",
            "PayableAmount": "-0.123456789012345678",
            "CouponAmount": "0.000000000000000001",
            "secret": "never return",
        }
        return httpx2.Response(
            200,
            json={"ResponseMetadata": {}, "Result": {"List": [item], "Total": 52, "Offset": 50}},
        )

    http = HttpClient()
    http.transport = lambda _: httpx2.MockTransport(respond)
    result = await BillingClient(http).bills(
        provider, CHANNELS[provider].api_base, keys().encoded().get_secret_value(), "2026-09", 50
    )
    assert len(result["items"]) == 1
    assert result["has_more"] is (provider == "volcengine")
    assert result["items"][0]["amounts"]["应付金额"] == "-0.123456789012345678"
    assert "secret" not in json.dumps(result)


@pytest.mark.parametrize(
    "provider,invalid",
    [
        ("aliyun_bailian", "error"),
        ("aliyun_bailian", "nan"),
        ("aliyun_bailian", "currency"),
        ("aliyun_bailian", "period"),
        ("aliyun_bailian", "oversize"),
        ("volcengine", "error"),
        ("volcengine", "currency"),
        ("volcengine", "period"),
        ("volcengine", "oversize"),
    ],
)
async def test_bill_response_rejects_errors_inexact_values_wrong_periods_and_unbounded_pages(
    provider, invalid
):
    ali = {
        "Success": True,
        "Data": {
            "BillingCycle": "2026-09",
            "Items": {
                "Item": [
                    {
                        "Currency": "CNY",
                        "ProductName": "百炼",
                        "ProductCode": "sfm",
                        "Item": "PayAsYouGoBill",
                        "PretaxAmount": "1",
                    }
                ]
            },
        },
    }
    volc = {
        "ResponseMetadata": {},
        "Result": {
            "Offset": 0,
            "Total": 1,
            "List": [
                {
                    "BillPeriod": "2026-09",
                    "Currency": "CNY",
                    "ProductZh": "方舟",
                    "Product": "ark",
                    "PayableAmount": "1",
                }
            ],
        },
    }
    payload = deepcopy(ali if provider == "aliyun_bailian" else volc)
    rows = (
        payload["Data"]["Items"]["Item"]
        if provider == "aliyun_bailian"
        else payload["Result"]["List"]
    )
    if invalid == "error":
        payload = {"Success": False, "ResponseMetadata": {"Error": {"Message": "secret"}}}
    elif invalid == "nan":
        rows[0]["PretaxAmount" if provider == "aliyun_bailian" else "PayableAmount"] = "NaN"
    elif invalid == "currency":
        rows[0]["Currency"] = "unknown"
    elif invalid == "period":
        if provider == "aliyun_bailian":
            payload["Data"]["BillingCycle"] = "2026-08"
        else:
            rows[0]["BillPeriod"] = "2026-08"
    else:
        rows.extend([rows[0]] * (10001 if provider == "aliyun_bailian" else 51))
    http = HttpClient()
    http.transport = lambda _: httpx2.MockTransport(lambda _: httpx2.Response(200, json=payload))
    with pytest.raises(ServiceError) as error:
        await BillingClient(http).bills(
            provider, CHANNELS[provider].api_base, keys().encoded().get_secret_value(), "2026-09", 0
        )
    assert error.value.code in {"model_billing_query_failed", "model_billing_invalid_response"}
    assert "secret" not in error.value.detail

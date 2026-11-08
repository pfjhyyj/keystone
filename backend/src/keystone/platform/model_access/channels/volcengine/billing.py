"""Volcengine account balances and product-level monthly bills, never per-call costs."""

import hashlib
import hmac
import json
from datetime import UTC, datetime

from keystone.platform.model_access.channels.shared.billing.cloud import (
    accounting_request,
    bill_amounts,
    bill_text,
    credentials,
    query_string,
)
from keystone.platform.model_access.channels.shared.billing.contracts import (
    ResponseBilling,
    balance_item,
)
from keystone.platform.model_access.schemas.errors import AdapterError


def volcengine_headers(
    keys, host, params, *, timestamp, body=b"", method="GET", region="cn-beijing"
):
    headers = {"host": host, "x-date": timestamp}
    if keys.security_token is not None:
        headers["x-security-token"] = keys.security_token.get_secret_value()
    signed = ";".join(sorted(headers))
    canonical_headers = "".join(name + ":" + headers[name] + "\n" for name in sorted(headers))
    canonical = "\n".join(
        (
            method,
            "/",
            query_string(params),
            canonical_headers,
            signed,
            hashlib.sha256(body).hexdigest(),
        )
    )
    scope = timestamp[:8] + "/" + region + "/billing/request"
    message = "\n".join(
        ("HMAC-SHA256", timestamp, scope, hashlib.sha256(canonical.encode()).hexdigest())
    )
    signing_key = keys.access_key_secret.get_secret_value().encode()
    for part in (timestamp[:8], region, "billing", "request"):
        signing_key = hmac.digest(signing_key, part.encode(), "sha256")
    signature = hmac.new(signing_key, message.encode(), hashlib.sha256).hexdigest()
    identifier = keys.access_key_id.get_secret_value()
    headers["Authorization"] = (
        f"HMAC-SHA256 Credential={identifier}/{scope}, "
        f"SignedHeaders={signed}, Signature={signature}"
    )
    if method == "POST":
        headers["content-type"] = "application/json"
    return headers


class Volcengine(ResponseBilling):
    provider = "volcengine"
    api_base = "https://ark.cn-beijing.volces.com/api/v3"
    billing_origin = "https://billing.volcengineapi.com"
    uses_billing_key = True
    enabled = True
    balance_path = "/"

    async def query(self, http, base, secret, action, *, body=None):
        if base != self.api_base or action not in {"QueryBalanceAcct", "ListBillDetail"}:
            raise AdapterError(409, "model_target_not_allowed", "云账务查询目标不受支持")
        params = {"Action": action, "Version": "2022-01-01"}
        content = json.dumps(body, separators=(",", ":")).encode() if body is not None else None
        headers = volcengine_headers(
            credentials(secret),
            "billing.volcengineapi.com",
            params,
            timestamp=datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"),
            method="POST" if content is not None else "GET",
            body=content or b"",
        )
        payload = await accounting_request(
            http, self.provider, self.billing_origin, params=params, headers=headers, body=content
        )
        metadata = payload.get("ResponseMetadata")
        if not isinstance(metadata, dict) or metadata.get("Error"):
            raise AdapterError(
                502, "model_billing_query_failed", "火山引擎账务查询失败，请核对账务授权"
            )
        return payload

    async def query_balance(self, http, base, secret):
        payload = await self.query(http, base, secret, "QueryBalanceAcct")
        try:
            data = payload["Result"]
            unit = data.get("Currency", "CNY")  # The China billing API reports RMB balances.
            if unit != "CNY":
                raise ValueError()
            fields = (
                ("AvailableBalance", "available"),
                ("CashBalance", "cash"),
                ("ArrearsBalance", "arrears"),
                ("CreditLimit", "credit"),
                ("FreezeAmount", "frozen"),
            )
            return {
                "scope": "volcengine_account",
                "items": [balance_item(kind, data[name], unit) for name, kind in fields],
            }
        except (ValueError, KeyError, TypeError):
            raise AdapterError(
                502, "model_billing_invalid_response", "火山引擎余额格式无效"
            ) from None

    async def bills(self, http, base, secret, period, offset):
        payload = await self.query(
            http,
            base,
            secret,
            "ListBillDetail",
            body={
                "BillPeriod": period,
                "GroupTerm": 2,
                "GroupPeriod": 0,
                "Limit": 50,
                "Offset": offset,
                "NeedRecordNum": 1,
                "IgnoreZero": 0,
            },
        )
        try:
            data = payload["Result"]
            rows, total = data["List"], data["Total"]
            if not isinstance(rows, list) or len(rows) > 50 or type(total) is not int or total < 0:
                raise ValueError()
            if data.get("Offset", offset) != offset or offset + len(rows) > total:
                raise ValueError()
            items = []
            for row in rows:
                if row["BillPeriod"] != period or row["Currency"] != "CNY":
                    raise ValueError()
                items.append(
                    {
                        "product": bill_text(row["ProductZh"]),
                        "product_code": bill_text(row["Product"]),
                        "category": bill_text(row.get("BillCategory", "")),
                        "unit": "CNY",
                        "amounts": bill_amounts(
                            row,
                            (
                                ("OriginalBillAmount", "原价"),
                                ("DiscountBillAmount", "折后价"),
                                ("PayableAmount", "应付金额"),
                                ("CouponAmount", "代金券抵扣"),
                                ("PaidAmount", "已付金额"),
                                ("UnpaidAmount", "未付金额"),
                            ),
                        ),
                    }
                )
            if not rows and offset < total:
                raise ValueError()
            return {"items": items, "has_more": offset + len(rows) < total}
        except (ValueError, KeyError, TypeError):
            raise AdapterError(
                502, "model_billing_invalid_response", "火山引擎账单格式无效"
            ) from None

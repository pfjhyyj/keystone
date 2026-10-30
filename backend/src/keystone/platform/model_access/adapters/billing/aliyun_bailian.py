from datetime import UTC, datetime
from uuid import uuid4

from keystone.platform.model_access.adapters.billing.cloud import (
    accounting_request,
    aliyun_signature,
    bill_amounts,
    bill_text,
    credentials,
)
from keystone.platform.model_access.adapters.billing.contracts import ResponseBilling, balance_item
from keystone.platform.model_access.schemas.errors import AdapterError


class AliyunBailian(ResponseBilling):
    provider = "aliyun_bailian"
    api_base = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    cost_query_mode = "none"
    cost_unit = None
    uses_billing_key = True
    enabled = True
    balance_path = "/"
    billing_origin = "https://business.aliyuncs.com"

    async def query(self, http, base, secret, action, **arguments):
        if base != self.api_base or action not in {"QueryAccountBalance", "QueryBillOverview"}:
            raise AdapterError(409, "model_target_not_allowed", "云账务查询目标不受支持")
        keys = credentials(secret)
        params = {
            "Action": action,
            "Version": "2017-12-14",
            "Format": "JSON",
            "AccessKeyId": keys.access_key_id.get_secret_value(),
            "SignatureMethod": "HMAC-SHA1",
            "SignatureVersion": "1.0",
            "SignatureNonce": str(uuid4()),
            "Timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            **arguments,
        }
        if keys.security_token is not None:
            params["SecurityToken"] = keys.security_token.get_secret_value()
        params["Signature"] = aliyun_signature(params, keys.access_key_secret.get_secret_value())
        payload = await accounting_request(http, self.provider, self.billing_origin, params=params)
        if payload.get("Success") is not True:
            raise AdapterError(
                502, "model_billing_query_failed", "阿里云账务查询失败，请核对账务授权"
            )
        return payload

    async def account_balance(self, http, base, secret):
        payload = await self.query(http, base, secret, "QueryAccountBalance")
        try:
            return self.balance(payload)
        except (ValueError, KeyError, TypeError):
            raise AdapterError(
                502, "model_billing_invalid_response", "阿里云余额格式无效"
            ) from None

    async def bills(self, http, base, secret, period, offset):
        payload = await self.query(http, base, secret, "QueryBillOverview", BillingCycle=period)
        try:
            data = payload["Data"]
            if data["BillingCycle"] != period:
                raise ValueError()
            items = data["Items"]["Item"]
            if not isinstance(items, list) or len(items) > 10000:
                raise ValueError()
            projected = []
            for item in items[offset : offset + 50]:
                unit = item["Currency"]
                if unit not in {"CNY", "USD", "JPY"}:
                    raise ValueError()
                projected.append(
                    {
                        "product": bill_text(item["ProductName"]),
                        "product_code": bill_text(item["ProductCode"]),
                        "category": bill_text(item["Item"]),
                        "unit": unit,
                        "amounts": bill_amounts(
                            item,
                            (
                                ("PretaxAmount", "应付金额"),
                                ("PaymentAmount", "现金支付（含退款抵扣）"),
                                ("CashAmount", "现金支付"),
                                ("OutstandingAmount", "未结清／信用结算"),
                                ("DeductedByCashCoupons", "代金券抵扣"),
                                ("InvoiceDiscount", "优惠金额"),
                            ),
                        ),
                    }
                )
            return {"items": projected, "has_more": offset + 50 < len(items)}
        except (ValueError, KeyError, TypeError):
            raise AdapterError(
                502, "model_billing_invalid_response", "阿里云账单格式无效"
            ) from None

    def balance(self, payload):
        data = payload["Data"]
        unit = data["Currency"]
        if unit not in {"CNY", "USD", "JPY"}:
            raise ValueError("Unsupported balance currency")
        return {
            "scope": "aliyun_account",
            "items": [
                balance_item("available", data["AvailableAmount"], unit),
                balance_item("cash", data["AvailableCashAmount"], unit),
            ],
        }

"""Standard NewAPI token logs: Bearer auth, gateway request IDs and recent-list responses."""

from decimal import Decimal, localcontext

from keystone.platform.model_access.channels.shared.billing.contracts import (
    BillingQueryError,
    Binding,
    LogPage,
    Receipt,
    balance_item,
)
from keystone.platform.model_access.channels.shared.billing.http import cost_get
from keystone.platform.model_access.channels.shared.billing.token_logs import (
    TokenLogs,
    quota_scale,
)
from keystone.platform.model_access.channels.shared.receipts import request_id
from keystone.platform.model_access.schemas.errors import AdapterError


class Newapi(TokenLogs):
    provider = "newapi"
    api_base = "https://newapiai.com/v1"
    quota_per_unit = None
    balance_path = "/api/usage/token/"

    def __init__(self, api_base=None):
        self.api_base = api_base or self.api_base
        self.billing_origin = self.api_base.removesuffix("/v1")

    def for_base(self, api_base):
        return Newapi(api_base)

    def binding(self, api_base):
        if api_base != self.api_base:
            raise ValueError("Mismatched NewAPI base")
        # NewAPI stores base USD quota; display currency and recharge price are separate.
        # The site's actual quota scale is read and retained in each reconciliation round.
        return Binding(
            self.billing_origin,
            "logs",
            "USD",
            None,
            "newapi-token-v1",
            # Preserve existing /v1 scopes; a different API base needs its own context.
            scope_origin=None
            if api_base.endswith("/v1")
            else self.billing_origin + "#newapi-token-v1:unversioned",
        )

    def receipt(self, identifiers, reported_cost):
        return Receipt(
            identifiers.get("x-oneapi-request-id") or identifiers.get("x-api-request-id")
        )

    def log_identifier(self, item):
        return request_id(item.get("request_id")) or super().log_identifier(item)

    async def validate_policy(self, http, context, secret):
        payload = await cost_get(http, self, context["origin"], "/api/status", limit=1048576)
        try:
            if context["unit"] != "USD":
                raise ValueError()
            return {"quota_per_unit": quota_scale(payload)}
        except (KeyError, TypeError, ValueError, OverflowError):
            raise BillingQueryError("cost_policy_changed", blocked=True) from None

    def check_target(self, base, path):
        if base != self.billing_origin or path not in {
            "/api/status",
            "/api/log/token",
            self.balance_path,
        }:
            raise AdapterError(409, "model_target_not_allowed", "账务查询目标不受支持")

    async def page(self, http, context, secret, *, start, end, page, page_size):
        payload = await cost_get(
            http,
            self,
            context["origin"],
            "/api/log/token",
            headers={"Authorization": "Bearer " + secret},
            params={
                "start_timestamp": start,
                "end_timestamp": end,
                "p": page,
                "page_size": page_size,
            },
            limit=4 * 1024 * 1024,
        )
        try:
            data = payload["data"]
            if isinstance(data, dict):
                records, complete = self.log_page(
                    payload, page=page, page_size=page_size, policy=context
                )
                return LogPage(records, page + 1, complete)
            if payload.get("success") is not True or not isinstance(data, list) or len(data) > 1000:
                raise ValueError()
            covered_since = None
            if len(data) == 1000:
                timestamps = [item["created_at"] for item in data]
                if any(type(value) is not int or not 0 <= value < 2**63 for value in timestamps):
                    raise ValueError()
                covered_since = min(timestamps)
            return LogPage(self.log_records(data, context), page + 1, True, covered_since)
        except (KeyError, TypeError, ValueError, OverflowError, RecursionError):
            raise BillingQueryError("cost_invalid_response", blocked=True) from None

    async def query_balance(self, http, base, secret):
        try:
            context = self.binding(base).snapshot("", self.provider, base)
            policy = await self.validate_policy(http, context, secret)
            payload = await cost_get(
                http,
                self,
                self.billing_origin,
                self.balance_path,
                headers={"Authorization": "Bearer " + secret},
                limit=65536,
            )
            data = payload["data"]
            if payload.get("code", payload.get("success")) is not True or not isinstance(
                data, dict
            ):
                raise ValueError()
            unlimited = data["unlimited_quota"]
            if type(unlimited) is not bool:
                raise ValueError()
            fields = (
                [("used", "total_used")]
                if unlimited
                else [
                    ("remaining", "total_available"),
                    ("used", "total_used"),
                    ("granted", "total_granted"),
                ]
            )
            token_items = quota_items(data, fields, policy["quota_per_unit"])
            if "user_quota" in data:
                account_items = quota_items(
                    data["user_quota"],
                    [("remaining", "total_available"), ("used", "total_used")],
                    policy["quota_per_unit"],
                )
                return {
                    "scope": "provider_account",
                    "unlimited": unlimited,
                    "items": account_items,
                    "token_items": token_items,
                }
            return {"scope": "token_quota", "unlimited": unlimited, "items": token_items}
        except (BillingQueryError, ValueError, KeyError, TypeError, OverflowError):
            raise AdapterError(502, "model_query_failed", "余额/额度查询失败") from None


def quota_items(data, fields, scale):
    if not isinstance(data, dict):
        raise ValueError()
    items = []
    with localcontext() as precision:
        precision.prec = 80
        for kind, field in fields:
            value = data[field]
            minimum = -(2**63) if kind == "remaining" else 0
            if type(value) is not int or not minimum <= value < 2**63:
                raise ValueError()
            items.append(balance_item(kind, Decimal(value) / Decimal(scale), "USD"))
    return items

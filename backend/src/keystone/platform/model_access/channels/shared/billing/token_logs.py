import json
from decimal import Decimal, localcontext

from keystone.platform.model_access.channels.shared.receipts import request_id

from .contracts import (
    BillingQueryError,
    Binding,
    LogPage,
    Receipt,
    ResponseBilling,
    amount,
)
from .http import cost_get


def quota_scale(payload):
    value = payload["data"]["quota_per_unit"]
    if (
        payload.get("success") is not True
        or isinstance(value, bool)
        or not isinstance(value, (int, Decimal))
        or isinstance(value, Decimal)
        and not value.is_finite()
        or not 0 < value <= 2**53
        or int(value) != value
    ):
        raise ValueError("Invalid quota scale")
    return int(value)


class TokenLogs(ResponseBilling):
    """Verified token log contract; response body IDs are not billing request IDs."""

    cost_query_mode = "logs"
    cost_unit = "USD"
    quota_per_unit = 500000
    enabled = True
    uses_billing_key = False
    balance_path = None

    def log_page(self, payload, *, page, page_size, policy):
        data = payload["data"]
        if payload.get("success") is not True or not isinstance(data, dict):
            raise ValueError("Invalid log envelope")
        items, total = data["items"], data["total"]
        if (
            not isinstance(items, list)
            or len(items) > page_size
            or type(total) is not int
            or total < 0
            or data.get("page") != page
            or data.get("page_size") != page_size
            or (len(items) < page_size and total > page * page_size)
            or (items and total < (page - 1) * page_size + len(items))
        ):
            raise ValueError("Invalid log pagination")
        return self.log_records(items, policy), len(items) < page_size or page * page_size >= total

    def log_identifier(self, item):
        other = item.get("other", {})
        if isinstance(other, str):
            if len(other.encode()) > 8192:
                raise ValueError("Oversized log metadata")
            other = json.loads(other)
        if not isinstance(other, dict):
            raise ValueError("Invalid log metadata")
        return request_id(other.get("request_id"))

    def log_records(self, items, policy):
        receipts = []
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("Invalid log")
            if item.get("type") != 2:
                continue
            identifier = self.log_identifier(item)
            if identifier is None:
                continue
            quota = item.get("quota")
            if type(quota) is not int or not 0 <= quota < 2**63:
                raise ValueError("Invalid quota")
            if (
                not isinstance(item.get("model_name"), str)
                or not 1 <= len(item["model_name"]) <= 128
            ):
                raise ValueError("Invalid model")
            with localcontext() as context:
                context.prec = 80
                cost = amount(Decimal(quota) / Decimal(policy["quota_per_unit"]))
            if cost is None:
                raise ValueError("Invalid converted quota")
            receipts.append(
                {
                    "request_id": identifier,
                    "amount": format(cost, "f"),
                    "unit": policy["unit"],
                    "quota": quota,
                    "quota_per_unit": policy["quota_per_unit"],
                    "model": item["model_name"],
                }
            )
        return receipts

    def binding(self, api_base):
        if api_base != self.api_base:
            raise ValueError("Fixed relay origin required")
        return Binding(
            self.billing_origin, "logs", self.cost_unit, self.quota_per_unit, "token-log-v1"
        )

    def receipt(self, identifiers, reported_cost):
        return Receipt(identifiers.get("x-api-request-id"))

    def check_target(self, base, path):
        from keystone.platform.model_access.schemas.errors import AdapterError

        if base != self.billing_origin or path not in {"/api/status", "/api/log/token"}:
            raise AdapterError(409, "model_target_not_allowed", "账务查询目标不受支持")

    def policy_matches(self, context, payload):
        data = payload.get("data")
        return (
            payload.get("success") is True
            and isinstance(data, dict)
            and data.get("quota_per_unit") == context["quota_per_unit"] == self.quota_per_unit
            and context["unit"] == self.cost_unit
            and data.get("currency_type") == self.currency_type
            and (
                self.currency_type != "CUSTOM"
                or data.get("custom_currency_exchange_rate") == self.exchange_rate
            )
        )

    async def validate_policy(self, http, context, secret):
        payload = await cost_get(http, self, context["origin"], "/api/status", limit=1048576)
        if not self.policy_matches(context, payload):
            raise BillingQueryError("cost_policy_changed", blocked=True)

    async def page(self, http, context, secret, *, start, end, page, page_size):
        payload = await cost_get(
            http,
            self,
            context["origin"],
            "/api/log/token",
            params={
                "key": secret,
                "start_timestamp": start,
                "end_timestamp": end,
                "p": page,
                "page_size": page_size,
            },
        )
        try:
            records, complete = self.log_page(
                payload, page=page, page_size=page_size, policy=context
            )
            return LogPage(records, page + 1, complete)
        except (ValueError, KeyError, TypeError, OverflowError, RecursionError):
            raise BillingQueryError("cost_invalid_response", blocked=True) from None

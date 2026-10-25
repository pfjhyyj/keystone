from keystone.platform.model_access.adapters.billing.contracts import BillingQueryError, Binding
from keystone.platform.model_access.adapters.billing.http import cost_get
from keystone.platform.model_access.adapters.billing.token_logs import TokenLogs, quota_scale


class Yunwu(TokenLogs):
    provider = "yunwu"
    api_base = "https://yunwu.ai/v1"
    billing_origin = "https://yunwu.ai"
    cost_unit = "yunwu_credits"
    currency_type = "CUSTOM"
    exchange_rate = 1

    def __init__(self, api_base=None):
        self.compatible = api_base is not None and api_base != self.api_base
        if self.compatible:
            self.api_base = api_base
            self.billing_origin = api_base.removesuffix("/v1")
            self.quota_per_unit = None

    def for_base(self, api_base):
        return Yunwu(api_base)

    def binding(self, api_base):
        if not self.compatible:
            return super().binding(api_base)
        if api_base != self.api_base:
            raise ValueError("Mismatched Yunwu-compatible base")
        return Binding(
            self.billing_origin,
            "logs",
            self.cost_unit,
            None,
            "yunwu-token-v1",
            scope_origin=self.billing_origin
            + "#yunwu-token-v1"
            + ("" if api_base.endswith("/v1") else ":unversioned"),
        )

    async def validate_policy(self, http, context, secret):
        if not self.compatible:
            return await super().validate_policy(http, context, secret)
        payload = await cost_get(http, self, context["origin"], "/api/status", limit=1048576)
        try:
            return {"quota_per_unit": quota_scale(payload)}
        except (ValueError, KeyError, TypeError, OverflowError):
            raise BillingQueryError("cost_policy_changed", blocked=True) from None

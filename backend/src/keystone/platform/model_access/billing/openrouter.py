from decimal import localcontext

from .contracts import (
    BillingQueryError,
    Binding,
    Fee,
    Receipt,
    ResponseBilling,
    amount,
    balance_item,
)
from .http import cost_get


class OpenRouter(ResponseBilling):
    provider = "openrouter"
    api_base = "https://openrouter.ai/api/v1"
    enabled = True
    cost_query_mode = "request"
    cost_unit = "credits"
    uses_billing_key = True
    balance_path = "/credits"

    def cost_query(self, identifier):
        return "/generation", {"id": identifier}

    def queried_cost(self, payload, identifier):
        data = payload["data"]
        if not isinstance(data, dict) or data.get("id") != identifier:
            raise ValueError("Mismatched generation")
        result = amount(data.get("total_cost"))
        if data.get("total_cost") is not None and result is None:
            raise ValueError("Invalid generation cost")
        return result

    def balance(self, payload):
        data = payload["data"]
        total, used = amount(data["total_credits"]), amount(data["total_usage"])
        if total is None or used is None:
            raise ValueError("Invalid credits")
        with localcontext() as context:
            context.prec = 80
            remaining = total - used
        return {
            "scope": "provider_account",
            "items": [balance_item("remaining", remaining, "credits")],
        }

    def binding(self, api_base):
        return Binding(api_base, self.cost_query_mode, self.cost_unit, None, "generation-v1")

    def receipt(self, identifiers, reported_cost):
        cost = amount(reported_cost)
        return Receipt(
            identifiers.get("body_id") or identifiers.get("x-request-id"),
            Fee(cost, self.cost_unit) if cost is not None else None,
        )

    def paths(self):
        return super().paths() | {"/generation"}

    async def lookup(self, http, context, secret, identifier):
        path, params = self.cost_query(identifier)
        try:
            payload = await cost_get(
                http,
                self,
                context["api_base"],
                path,
                params=params,
                headers={"Authorization": "Bearer " + secret},
                missing=True,
            )
            cost = self.queried_cost(payload, identifier)
            return Fee(cost, context["unit"]) if cost is not None else None
        except BillingQueryError as error:
            if error.missing:
                return None
            raise
        except (ValueError, KeyError, TypeError):
            raise BillingQueryError("cost_invalid_response", blocked=True) from None

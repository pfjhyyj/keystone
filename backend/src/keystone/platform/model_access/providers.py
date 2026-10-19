"""Small provider receipt mappers. No price tables, token estimates or raw payload storage."""

import re
from decimal import Decimal, InvalidOperation, localcontext


def amount(value, *, signed=False):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
        if (
            not result.is_finite()
            or result.copy_abs() >= Decimal("1e20")
            or result.as_tuple().exponent < -18
            or (not signed and result < 0)
        ):
            return None
        return result
    except (InvalidOperation, ValueError, TypeError):
        return None


def request_id(value):
    return (
        value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", value) else None
    )


def usage(data):
    if not isinstance(data, dict):
        return None
    result = {}
    fields = {
        "prompt_tokens": "input_tokens",
        "completion_tokens": "output_tokens",
        "total_tokens": "total_tokens",
        "prompt_cache_hit_tokens": "cache_read_input_tokens",
        "prompt_cache_miss_tokens": "cache_miss_input_tokens",
    }
    for source, target in fields.items():
        value = data.get(source)
        if type(value) is int and 0 <= value < 2**63:
            result[target] = value
    for source, detail, target in (
        ("prompt_tokens_details", "cached_tokens", "cache_read_input_tokens"),
        ("completion_tokens_details", "reasoning_tokens", "reasoning_tokens"),
    ):
        extra = data.get(source)
        value = extra.get(detail) if isinstance(extra, dict) else None
        if type(value) is int and 0 <= value < 2**63:
            result[target] = value
    return result or None


class DeepSeek:
    cost_unit = None
    supports_cost_query = False
    uses_billing_key = False
    balance_path = "/user/balance"

    def cost(self, usage_data):
        return None

    def balance(self, payload):
        items = []
        for entry in payload["balance_infos"]:
            unit = entry["currency"]
            if unit not in {"CNY", "USD"}:
                raise ValueError("Unsupported balance currency")
            for source, kind in (
                ("total_balance", "total"),
                ("granted_balance", "granted"),
                ("topped_up_balance", "topped_up"),
            ):
                items.append(balance_item(kind, entry[source], unit))
        if not items or len(items) > 12:
            raise ValueError("Invalid balance items")
        return {"scope": "provider_account", "items": items}


class OpenRouter:
    cost_unit = "credits"
    supports_cost_query = True
    uses_billing_key = True
    balance_path = "/credits"

    def cost(self, usage_data):
        return amount(usage_data.get("cost")) if isinstance(usage_data, dict) else None

    def cost_query(self, identifier):
        return "/generation", {"id": identifier}

    def queried_cost(self, payload, identifier):
        data = payload["data"]
        if data.get("id") != identifier:
            raise ValueError("Mismatched generation")
        return amount(data.get("total_cost"))

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


class AliyunBailian:
    cost_unit = None
    supports_cost_query = False
    uses_billing_key = True
    # Mock contract only. Real Alibaba OpenAPI signing/region selection is not enabled.
    balance_path = "/mock/QueryAccountBalance"

    def cost(self, usage_data):
        return None

    def balance(self, payload):
        data = payload["Data"]
        unit = data["Currency"]
        if unit not in {"CNY", "USD"}:
            raise ValueError("Unsupported balance currency")
        return {
            "scope": "aliyun_account",
            "items": [
                balance_item("available", data["AvailableAmount"], unit),
                balance_item("cash", data["AvailableCashAmount"], unit),
            ],
        }


def balance_item(kind, value, unit):
    parsed = amount(value, signed=True)
    if parsed is None:
        raise ValueError("Invalid amount")
    return {"kind": kind, "amount": format(parsed, "f"), "unit": unit}


PROVIDERS = {"deepseek": DeepSeek(), "openrouter": OpenRouter(), "aliyun_bailian": AliyunBailian()}

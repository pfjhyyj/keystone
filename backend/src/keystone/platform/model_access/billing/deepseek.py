from .contracts import ResponseBilling, balance_item


class DeepSeek(ResponseBilling):
    provider = "deepseek"
    api_base = "https://api.deepseek.com"
    cost_query_mode = "none"
    cost_unit = None
    uses_billing_key = False
    balance_path = "/user/balance"

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

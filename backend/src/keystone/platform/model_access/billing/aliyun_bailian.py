from .contracts import ResponseBilling, balance_item


class AliyunBailian(ResponseBilling):
    provider = "aliyun_bailian"
    api_base = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    cost_query_mode = "none"
    cost_unit = None
    uses_billing_key = True
    # Alibaba OpenAPI signing/region selection is not enabled.
    balance_path = None

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

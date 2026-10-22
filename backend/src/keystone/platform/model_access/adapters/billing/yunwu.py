from keystone.platform.model_access.adapters.billing.token_logs import TokenLogs


class Yunwu(TokenLogs):
    provider = "yunwu"
    api_base = "https://yunwu.ai/v1"
    billing_origin = "https://yunwu.ai"
    cost_unit = "yunwu_credits"
    currency_type = "CUSTOM"
    exchange_rate = 1

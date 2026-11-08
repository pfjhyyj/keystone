from keystone.platform.model_access.channels.shared.billing.token_logs import TokenLogs


class Openlux(TokenLogs):
    provider = "openlux"
    api_base = "https://api.openlux.ai/v1"
    billing_origin = "https://api.openlux.ai"
    cost_unit = "USD"
    currency_type = "USD"

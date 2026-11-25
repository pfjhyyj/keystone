"""OpenAI official API: no per-request cost or balance query with an inference key."""

from keystone.platform.model_access.channels.shared.billing.contracts import ResponseBilling


class OpenAI(ResponseBilling):
    provider = "openai"
    api_base = "https://api.openai.com/v1"

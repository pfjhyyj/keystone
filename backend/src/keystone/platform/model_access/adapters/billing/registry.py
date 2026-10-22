"""Explicit billing capabilities; no ORM, secrets or background registration."""

from keystone.platform.model_access.adapters.billing.aliyun_bailian import AliyunBailian
from keystone.platform.model_access.adapters.billing.deepseek import DeepSeek
from keystone.platform.model_access.adapters.billing.http import balance_get
from keystone.platform.model_access.adapters.billing.openlux import Openlux
from keystone.platform.model_access.adapters.billing.openrouter import OpenRouter
from keystone.platform.model_access.adapters.billing.yunwu import Yunwu

PROVIDERS = {
    "deepseek": DeepSeek(),
    "openrouter": OpenRouter(),
    "aliyun_bailian": AliyunBailian(),
    "openlux": Openlux(),
    "yunwu": Yunwu(),
}


class BillingClient:
    def __init__(self, http):
        self.http = http

    def adapter(self, provider):
        return PROVIDERS[provider]

    async def validate_policy(self, provider, context, secret):
        await self.adapter(provider).validate_policy(self.http, context, secret)

    async def page(self, provider, context, secret, **window):
        return await self.adapter(provider).page(self.http, context, secret, **window)

    async def lookup(self, provider, context, secret, identifier):
        return await self.adapter(provider).lookup(self.http, context, secret, identifier)

    async def balance(self, provider, base, secret):
        return await balance_get(self.http, self.adapter(provider), base, secret)

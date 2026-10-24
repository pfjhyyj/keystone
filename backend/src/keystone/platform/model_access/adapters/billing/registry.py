"""Keystone billing facade over the installed channel compositions."""

from keystone.core.errors import ServiceError
from keystone.platform.model_access.adapters.billing.contracts import BillingQueryError
from keystone.platform.model_access.adapters.billing.http import balance_get
from keystone.platform.model_access.adapters.channels import CHANNELS
from keystone.platform.model_access.adapters.errors import adapter_errors


class BillingClient:
    def __init__(self, http, channels=None):
        self.http = http
        self.channels = channels if channels is not None else CHANNELS

    def adapter(self, provider):
        channel = self.channels.get(provider)
        if channel is None:
            raise ServiceError(503, "model_provider_unavailable", "当前服务商尚未开放账务查询")
        return channel.billing

    def operation(self, provider, name):
        method = getattr(self.adapter(provider), name, None)
        if method is None:
            raise BillingQueryError("cost_query_unavailable", blocked=True)
        return method

    async def validate_policy(self, provider, context, secret):
        await self.operation(provider, "validate_policy")(self.http, context, secret)

    async def page(self, provider, context, secret, **window):
        return await self.operation(provider, "page")(self.http, context, secret, **window)

    async def lookup(self, provider, context, secret, identifier):
        return await self.operation(provider, "lookup")(self.http, context, secret, identifier)

    async def balance(self, provider, base, secret):
        with adapter_errors():
            return await balance_get(self.http, self.adapter(provider), base, secret)

"""Keystone billing facade over the installed channel compositions."""

from keystone.core.errors import ServiceError
from keystone.platform.model_access.adapters.billing.contracts import BillingQueryError
from keystone.platform.model_access.adapters.billing.http import balance_get
from keystone.platform.model_access.adapters.channels import CHANNELS, channel_adapter
from keystone.platform.model_access.adapters.errors import adapter_errors
from keystone.platform.model_access.schemas.errors import AdapterError


class BillingClient:
    def __init__(self, http, channels=None):
        self.http = http
        self.channels = channels if channels is not None else CHANNELS

    def adapter(self, provider, base=None):
        channel = channel_adapter(provider, base, channels=self.channels)
        if channel is None:
            raise ServiceError(503, "model_provider_unavailable", "当前服务商尚未开放账务查询")
        return channel.billing

    def operation(self, provider, name, base=None):
        method = getattr(self.adapter(provider, base), name, None)
        if method is None:
            raise BillingQueryError("cost_query_unavailable", blocked=True)
        return method

    async def validate_policy(self, provider, context, secret):
        return await self.operation(provider, "validate_policy", context.get("api_base"))(
            self.http, context, secret
        )

    async def page(self, provider, context, secret, **window):
        return await self.operation(provider, "page", context.get("api_base"))(
            self.http, context, secret, **window
        )

    async def lookup(self, provider, context, secret, identifier):
        return await self.operation(provider, "lookup", context.get("api_base"))(
            self.http, context, secret, identifier
        )

    async def balance(self, provider, base, secret):
        with adapter_errors():
            adapter = self.adapter(provider, base)
            query = getattr(adapter, "account_balance", None) or getattr(
                adapter, "token_balance", None
            )
            if query is not None:
                return await query(self.http, base, secret)
            return await balance_get(self.http, adapter, base, secret)

    async def bills(self, provider, base, secret, period, offset):
        with adapter_errors():
            query = getattr(self.adapter(provider, base), "bills", None)
            if query is None:
                raise AdapterError(409, "model_billing_unavailable", "当前渠道未接入云账户月账单")
            return await query(self.http, base, secret, period, offset)

"""Keystone billing facade over the installed channel compositions."""

from keystone.core.errors import ServiceError
from keystone.platform.model_access.adapters.errors import adapter_errors
from keystone.platform.model_access.channels import CHANNELS, channel_adapter
from keystone.platform.model_access.channels.shared.billing.contracts import BillingQueryError
from keystone.platform.model_access.channels.spec import (
    BalanceQuery,
    CloudBills,
    CostLogs,
    RequestCost,
)
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

    def capability(self, provider, capability, base=None):
        adapter = self.adapter(provider, base)
        if not isinstance(adapter, capability):
            raise BillingQueryError("cost_query_unavailable", blocked=True)
        return adapter

    async def validate_policy(self, provider, context, secret):
        return await self.capability(provider, CostLogs, context.get("api_base")).validate_policy(
            self.http, context, secret
        )

    async def page(self, provider, context, secret, **window):
        return await self.capability(provider, CostLogs, context.get("api_base")).page(
            self.http, context, secret, **window
        )

    async def lookup(self, provider, context, secret, identifier):
        return await self.capability(provider, RequestCost, context.get("api_base")).lookup(
            self.http, context, secret, identifier
        )

    async def balance(self, provider, base, secret):
        with adapter_errors():
            adapter = self.adapter(provider, base)
            if not isinstance(adapter, BalanceQuery):
                raise AdapterError(503, "model_provider_unavailable", "当前服务商尚未开放账务查询")
            return await adapter.query_balance(self.http, base, secret)

    async def bills(self, provider, base, secret, period, offset):
        with adapter_errors():
            adapter = self.adapter(provider, base)
            if not isinstance(adapter, CloudBills):
                raise AdapterError(409, "model_billing_unavailable", "当前渠道未接入云账户月账单")
            return await adapter.bills(self.http, base, secret, period, offset)

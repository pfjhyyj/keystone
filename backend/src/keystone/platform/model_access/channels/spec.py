"""Declared channel composition. Installed channels are code, never database configuration.

Generic layers read these declarations instead of branching on provider identifiers.
"""

from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field, replace
from typing import Protocol, runtime_checkable


@runtime_checkable
class BalanceQuery(Protocol):
    async def query_balance(self, http, base, secret): ...


@runtime_checkable
class CostLogs(Protocol):
    async def validate_policy(self, http, context, secret): ...

    async def page(self, http, context, secret, *, start, end, page, page_size): ...


@runtime_checkable
class RequestCost(Protocol):
    async def lookup(self, http, context, secret, identifier): ...


@runtime_checkable
class CloudBills(Protocol):
    async def bills(self, http, base, secret, period, offset): ...


@runtime_checkable
class CustomBaseBilling(Protocol):
    def for_base(self, base): ...


class ModelDiscovery(Protocol):
    async def __call__(self, http, provider, secret, workspace_id, check): ...


REFERENCE_KEY = ("provider", "api_base", "provider_model", "operation", "delivery")


def bearer_auth(request, secret):
    request.headers["Authorization"] = "Bearer " + secret


@dataclass(frozen=True)
class ChannelField:
    """An extra non-secret channel setting, persisted in a column of the same name."""

    name: str
    label: str
    hint: str = ""
    placeholder: str = ""
    pattern: str | None = None


@dataclass(frozen=True)
class ChannelSpec:
    id: str
    label: str
    api_base: str
    protocols: tuple
    billing: object
    authorize: Callable = bearer_auth
    models_path: str | None = None
    # Optional custom model listing: (http, provider, secret, workspace_id, check) -> items.
    discover: ModelDiscovery | None = None
    # Historical channels still resolve but cannot be created from the console.
    creatable: bool = True
    # Console grouping for channels that share one user-facing type.
    display_as: str | None = None
    # Compatible relays accept an administrator-provided API base.
    custom_base: bool = False
    custom_protocols: tuple | None = None
    # Installed sites that a custom base resolves to, checked in this order.
    legacy_sites: tuple = ()
    # Balance and bills use separate cloud accounting credentials.
    cloud_billing: bool = False
    fields: tuple = ()
    hint: str = ""
    reference_prices: Mapping = field(default_factory=dict)
    # Request context for which the reference tariff is valid; None accepts any context.
    reference_applies: Callable | None = None
    pricing_profiles: tuple = ()
    pricing_context_defaults: Mapping = field(default_factory=dict)

    def protocol(self, operation, delivery=None, *, model=None):
        return next(
            (
                protocol
                for protocol in self.protocols
                if operation in protocol.operations
                and (delivery is None or delivery == protocol.delivery)
                and (model is None or protocol.models is None or model in protocol.models)
            ),
            None,
        )

    def supports(self, operation, delivery, *, model=None):
        return self.protocol(operation, delivery, model=model) is not None

    def allows_target(self, base, path, method):
        return base == self.api_base and any(
            protocol.paths.get(path) == method for protocol in self.protocols
        )

    def for_base(self, base):
        """The same channel bound to an administrator-provided compatible API base."""

        return replace(
            self,
            api_base=base,
            protocols=self.custom_protocols
            if self.custom_protocols is not None
            else self.protocols,
            billing=self.billing.for_base(base)
            if isinstance(self.billing, CustomBaseBilling)
            else self.billing,
        )

    def catalog_adapter(self):
        """Capabilities of an unknown custom base, for catalog predicates."""
        if self.custom_base and self.custom_protocols is not None:
            return replace(self, protocols=self.custom_protocols)
        return self

    def reference(self, binding, context=None):
        if context is not None and self.reference_applies and not self.reference_applies(context):
            return None
        return self.reference_prices.get(tuple(binding[key] for key in REFERENCE_KEY))

    @property
    def balance_available(self):
        return bool(
            isinstance(self.billing, BalanceQuery)
            and self.billing.enabled
            and self.billing.balance_path
        )

    @property
    def bills_available(self):
        return isinstance(self.billing, CloudBills)

    @property
    def billing_credentials(self):
        if self.cloud_billing:
            return "cloud"
        return "key" if self.billing.uses_billing_key else "none"

    def describe(self):
        return {
            "id": self.id,
            "label": self.label,
            "display_as": self.display_as or self.id,
            "creatable": self.creatable,
            "default_api_base": self.api_base,
            "custom_base": self.custom_base,
            "balance": self.balance_available,
            "bills": self.bills_available,
            "billing_credentials": self.billing_credentials,
            "fields": [asdict(item) for item in self.fields],
            "hint": self.hint,
        }

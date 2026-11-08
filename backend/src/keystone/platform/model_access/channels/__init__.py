"""Installed channel registry. Adding a channel: create its package and list its SPEC here.

Historical provider IDs remain valid in saved channels, billing evidence and Flow snapshots,
so a removed channel should be kept with creatable=False rather than deleted.
"""

from keystone.platform.model_access.channels import (
    aliyun_bailian,
    deepseek,
    newapi,
    openlux,
    openrouter,
    volcengine,
    yunwu,
)
from keystone.platform.model_access.schemas.channels import normalize_api_base

# Console order; legacy channels follow the creatable ones.
INSTALLED = (
    openrouter.SPEC,
    newapi.SPEC,
    yunwu.SPEC,
    aliyun_bailian.SPEC,
    volcengine.SPEC,
    openlux.SPEC,
    deepseek.SPEC,
)
CHANNELS = {spec.id: spec for spec in INSTALLED}
REFERENCE_PRICES = {
    key: price for spec in INSTALLED for key, price in spec.reference_prices.items()
}


def channel_adapter(provider, base=None, *, channels=None):
    installed = CHANNELS if channels is None else channels
    spec = installed.get(provider)
    if spec is None or base is None or not spec.custom_base:
        return spec
    try:
        if normalize_api_base(base) != base:
            return None
    except (ValueError, UnicodeError):
        return None
    for legacy in spec.legacy_sites:
        site = installed.get(legacy)
        if site is not None and base == site.api_base:
            return site
    return spec.for_base(base)


def channel_targets():
    """Catalog predicates: compatible chat/vision plus site-specific image generation."""
    for provider, spec in CHANNELS.items():
        yield provider, None if spec.custom_base else spec.api_base, spec.catalog_adapter()
    for provider, spec in CHANNELS.items():
        for legacy in spec.legacy_sites if spec.custom_base else ():
            site = CHANNELS.get(legacy)
            if site is not None:
                yield provider, site.api_base, site


def channel_options():
    sites = {}
    for provider, spec in CHANNELS.items():
        for identifier in spec.legacy_sites if spec.custom_base else ():
            site = CHANNELS[identifier]
            if site.api_base not in sites:
                sites[site.api_base] = {
                    "api_base": site.api_base,
                    "providers": [],
                    "display_as": site.display_as or site.id,
                    "balance": site.balance_available,
                    "bills": site.bills_available,
                    "billing_credentials": site.billing_credentials,
                }
            sites[site.api_base]["providers"].append(provider)
    return {
        "channels": [spec.describe() for spec in CHANNELS.values()],
        "legacy_sites": list(sites.values()),
    }


def reference(binding, *, context=None):
    spec = CHANNELS.get(binding["provider"])
    return spec.reference(binding, context) if spec is not None else None

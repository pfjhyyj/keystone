"""Resolve management configuration and freeze it inside the send transaction."""

from decimal import Decimal

from keystone.core.errors import ServiceError
from keystone.core.time import utcnow
from keystone.platform.model_access.adapters.channels import channel_adapter
from keystone.platform.model_access.adapters.inference.pricing import (
    PROFILES,
    normalize_pricing_facts,
    observed_time_context,
    profile_for,
)
from keystone.platform.model_access.adapters.pricing.catalog import reference
from keystone.platform.model_access.models import Channel, ModelEntry
from keystone.platform.model_access.schemas.pricing import (
    EstimateResult,
    PricingConfig,
    PricingFacts,
    PricingSnapshot,
    digest,
)
from keystone.platform.model_access.services.pricing_calculator import (
    estimate,
    validate_context,
    validate_rule,
)
from keystone.platform.model_access.services.queries import locked


def target_binding(entry, channel):
    return {
        "channel_id": str(channel.id),
        "provider": channel.provider,
        "api_base": channel.api_base,
        "provider_model": entry.provider_model,
    }


def installed_profiles(entry, channel):
    adapter = channel_adapter(channel.provider, channel.api_base)
    return {
        (operation, delivery): profile_for(protocol)
        for operation in entry.operations
        for delivery in ("sync", "async")
        if adapter
        and (protocol := adapter.protocol(operation, delivery, model=entry.provider_model))
    }


def freeze_pricing(entry, channel, operation, delivery, profile, context):
    binding = {**target_binding(entry, channel), "operation": operation, "delivery": delivery}
    base = {
        "binding": binding,
        "revision": entry.pricing_revision or 0,
        "profile": profile.id,
        "context": context,
    }
    try:
        stored = entry.pricing or {"mode": "inherit"}
        config = PricingConfig.model_validate({k: v for k, v in stored.items() if k != "binding"})
        if config.mode == "disabled":
            return PricingSnapshot(**base, reason="pricing_disabled")
        if config.mode == "override" and stored.get("binding") != target_binding(entry, channel):
            return PricingSnapshot(**base, reason="pricing_binding_changed")
        price = next(
            (r for r in config.rules if (r.operation, r.delivery) == (operation, delivery)), None
        )
        price = price or reference(binding, context=context)
        if price is None:
            return PricingSnapshot(**base, reason="price_missing")
        validate_rule(price.rule, profile)
        validate_context(context, profile, time_basis=price.rule.time_basis)
        return PricingSnapshot(**base, rule=price.rule, source=price.source)
    except Exception:
        return PricingSnapshot(**{**base, "context": {}}, reason="pricing_invalid")


def apply_estimate(row, facts=None, *, pending=False):
    """Restricted finalization: a pricing failure cannot replace the inference outcome."""
    try:
        snapshot = getattr(row, "estimate_snapshot", None)
        if snapshot is None:
            result = EstimateResult(status="unavailable", reason="legacy_no_snapshot")
        else:
            profile = PROFILES.get(snapshot.get("profile"))
            if profile is None:
                result = EstimateResult(status="invalid", reason="profile_version_unsupported")
            else:
                facts = PricingFacts.model_validate(
                    facts or row.estimate_facts or {"profile": profile.id}
                )
                row.estimate_facts = facts.model_dump(mode="json")
                result = estimate(snapshot, facts, profile)
                if pending and result.status == "partial":
                    result = result.model_copy(update={"status": "pending"})
        result = EstimateResult.model_validate(result)
        amount = Decimal(result.amount) if result.amount is not None else None
    except Exception:
        # No exception text, raw response or current-price retry enters the ledger.
        result = EstimateResult(status="invalid", reason="pricing_invalid")
        amount = None
    row.estimate_status, row.estimate_reason = result.status, result.reason
    row.estimate_amount = amount
    row.estimate_currency, row.estimate_detail = result.currency, result.detail
    row.estimate_checked_at = utcnow()


def finish_observation(row, observation, *, pending=False):
    try:
        profile = observation.pricing_profile
        if profile is None:
            apply_estimate(row, pending=pending)
            return
        response_complete = observation.pricing_observed_at is not None
        complete = response_complete and (
            not observation.streaming or observation.pricing_usage_final
        )
        facts = normalize_pricing_facts(
            profile,
            observation.pricing_usage,
            complete=complete,
            success=response_complete and observation.pricing_success,
            generated=observation.pricing_generated if response_complete else None,
        )
        snapshot = PricingSnapshot.model_validate(row.estimate_snapshot)
        facts.context = observed_time_context(snapshot.rule, observation.pricing_observed_at)
        apply_estimate(row, facts, pending=pending)
    except Exception:
        row.estimate_status, row.estimate_reason = "invalid", "pricing_invalid"
        row.estimate_amount = None
        row.estimate_checked_at = utcnow()


class PricingService:
    def __init__(self, identity):
        self.identity = identity

    @staticmethod
    def validate(config, entry, channel):
        profiles = installed_profiles(entry, channel)
        try:
            for price in config.rules:
                profile = profiles.get((price.operation, price.delivery))
                if profile is None:
                    raise ValueError("Operation/delivery has no installed meter")
                validate_rule(price.rule, profile)
        except ValueError:
            raise ServiceError(
                422, "pricing_invalid", "价格规则与已安装操作、计量字段或分区不兼容"
            ) from None
        return profiles

    @staticmethod
    def view(entry, channel):
        stored = entry.pricing or {"mode": "inherit", "rules": []}
        profiles = installed_profiles(entry, channel)
        return {
            "revision": entry.pricing_revision,
            "config": {k: v for k, v in stored.items() if k != "binding"},
            "binding_valid": stored.get("binding", target_binding(entry, channel))
            == target_binding(entry, channel),
            "operations": [
                {
                    "operation": operation,
                    "delivery": delivery,
                    "profile": profile.model_dump(),
                    "reference": (
                        price.model_dump(mode="json", by_alias=True)
                        if (
                            price := reference(
                                {
                                    **target_binding(entry, channel),
                                    "operation": operation,
                                    "delivery": delivery,
                                }
                            )
                        )
                        else None
                    ),
                }
                for (operation, delivery), profile in profiles.items()
            ],
        }

    async def get(self, identifier):
        async with self.identity.access("model:manage") as (session, _):
            entry = await locked(session, ModelEntry, identifier)
            channel = await locked(session, Channel, entry.channel_id)
            return self.view(entry, channel)

    async def save(self, identifier, data):
        async with self.identity.access(
            "model:manage",
            action="model.pricing.update",
            target="model.entry",
            target_id=identifier,
        ) as (session, auth):
            entry = await locked(session, ModelEntry, identifier)
            channel = await locked(session, Channel, entry.channel_id)
            if entry.pricing_revision != data.expected_revision:
                raise ServiceError(
                    409, "pricing_revision_conflict", "价格已被修改，请重新加载后编辑"
                )
            self.validate(data.config, entry, channel)
            before = digest(entry.pricing)
            config = data.config.model_dump(mode="json", by_alias=True)
            if data.config.mode == "override":
                config["binding"] = target_binding(entry, channel)
            entry.pricing, entry.pricing_revision = config, entry.pricing_revision + 1
            self.identity.touch(entry, auth)
            await self.identity.log(
                session,
                auth,
                "model.pricing.update",
                "model.entry",
                entry.id,
                {
                    "before_hash": before,
                    "after_hash": digest(config),
                    "revision": entry.pricing_revision,
                },
            )
            await session.flush()
            return self.view(entry, channel)

    async def preview(self, identifier, data):
        async with self.identity.access("model:manage") as (session, _):
            entry = await locked(session, ModelEntry, identifier)
            channel = await locked(session, Channel, entry.channel_id)
            profiles = self.validate(data.config, entry, channel)
            profile = profiles.get((data.operation, data.delivery))
            if profile is None:
                raise ServiceError(422, "pricing_invalid", "该操作没有已安装的计量规范")
            # Never mutate the ORM entry, even transiently: SQLAlchemy could autoflush it.
            from types import SimpleNamespace

            draft = SimpleNamespace(
                provider_model=entry.provider_model,
                pricing_revision=entry.pricing_revision,
                pricing={
                    **data.config.model_dump(mode="json", by_alias=True),
                    "binding": target_binding(entry, channel),
                },
            )
            try:
                validate_context(
                    data.context,
                    profile,
                    time_basis=next(
                        (
                            r.rule.time_basis
                            for r in data.config.rules
                            if (r.operation, r.delivery) == (data.operation, data.delivery)
                        ),
                        None,
                    ),
                )
            except ValueError:
                raise ServiceError(
                    422, "pricing_invalid", "试算条件包含不支持的字段或类型"
                ) from None
            snapshot = freeze_pricing(
                draft, channel, data.operation, data.delivery, profile, data.context
            )
            facts = data.facts.model_copy(deep=True)
            for measurement in facts.values.values():
                measurement.source = "manual"
            return {
                **estimate(snapshot, facts, profile).model_dump(mode="json"),
                "simulation": True,
            }

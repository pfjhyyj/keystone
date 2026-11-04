"""Pure, deterministic interpretation of the bounded pricing tree."""

from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal, DecimalException, localcontext
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from keystone.platform.model_access.schemas.pricing import (
    Branch,
    EstimateResult,
    MetricProfile,
    Price,
    PriceRule,
    PricingFacts,
    PricingSnapshot,
    digest,
)

TIME_FIELDS = {
    "context.pricing_hour": "integer",
    "context.pricing_weekday": "integer",
    "context.pricing_date": "string",
}
# Covers 16 independent 12-digit denominators plus rate/factor scales and final
# 18-place rounding, including sums arbitrarily close to a half-even boundary.
CALCULATION_PRECISION = 320


def validate_context(context, profile, *, time_basis=None):
    fields = {**profile.context_fields, **(TIME_FIELDS if time_basis else {})}
    for key, value in context.items():
        kind = fields.get(f"context.{key}")
        if kind is None or type(value) is not (int if kind == "integer" else str):
            raise ValueError("Unsupported pricing context")
        if isinstance(value, int) and not 0 <= value < 2**63:
            raise ValueError("Invalid pricing context count")
        if (
            f"context.{key}" in profile.context_enums
            and value not in profile.context_enums[f"context.{key}"]
        ):
            raise ValueError("Unsupported pricing context value")
        if (
            key == "pricing_hour"
            and not 0 <= value <= 23
            or key == "pricing_weekday"
            and not 0 <= value <= 6
        ):
            raise ValueError("Invalid local pricing time")
        if key == "pricing_date":
            date.fromisoformat(value)


def validate_rule(rule: PriceRule | dict, profile: MetricProfile):
    rule = PriceRule.model_validate(rule)
    fields = {**profile.context_fields, **{k: "integer" for k in profile.quantities}}
    if rule.time_basis:
        try:
            ZoneInfo(rule.time_basis.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Unknown pricing timezone") from None
        fields.update(TIME_FIELDS)
    ids = set()

    def condition(predicate):
        children = predicate.all or predicate.any
        if children:
            for child in children:
                condition(child)
            return
        kind = fields.get(predicate.field)
        values = predicate.value if predicate.op == "in" else [predicate.value]
        if kind is None or any(type(v) is not (int if kind == "integer" else str) for v in values):
            raise ValueError("Unsupported predicate field or type")
        if kind == "string" and predicate.op not in {"eq", "in"}:
            raise ValueError("String fields support equality and membership only")
        if kind == "integer" and any(not 0 <= v < 2**63 for v in values):
            raise ValueError("Predicate quantity is outside limits")
        if predicate.field in profile.context_enums and any(
            v not in profile.context_enums[predicate.field] for v in values
        ):
            raise ValueError("Unsupported predicate enumeration")

    def visit(node):
        if isinstance(node, Branch):
            condition(node.when)
            visit(node.then)
            visit(node.otherwise)
            return
        if node.id in ids:
            raise ValueError("Duplicate price id")
        ids.add(node.id)
        expected = set()
        for partition, options in (
            (node.input_partition, profile.input_partitions),
            (node.output_partition, profile.output_partitions),
        ):
            if partition not in options:
                raise ValueError("Token partition is unavailable for this protocol")
            expected.update(options[partition])
        quantities, item_ids = set(), set()
        for item in node.items:
            if item.id in item_ids or item.quantity in quantities:
                raise ValueError("Duplicate price item")
            quantities.add(item.quantity)
            item_ids.add(item.id)
            unit = profile.quantities.get(item.quantity)
            if unit is None:
                raise ValueError("Unsupported quantity or billing unit")
            if unit == "token" and item.quantity not in expected:
                raise ValueError("Overlapping or mismatched token partition")
        if not expected <= quantities:
            raise ValueError("Every token partition component needs an explicit price")

    visit(rule.root)
    return rule


class MissingCondition(Exception):
    def __init__(self, fields):
        self.fields = fields


def _predicate(predicate, values):
    children = predicate.all or predicate.any
    if children:
        results, missing = [], set()
        for child in children:
            try:
                results.append(_predicate(child, values))
            except MissingCondition as error:
                missing.update(error.fields)
        # Even a logically redundant missing condition is never silently discarded.
        if missing:
            raise MissingCondition(sorted(missing))
        return all(results) if predicate.all else any(results)
    if predicate.field not in values:
        raise MissingCondition([predicate.field])
    actual, expected = values[predicate.field], predicate.value
    if predicate.op == "in":
        return actual in expected
    return {
        "eq": lambda: actual == expected,
        "lt": lambda: actual < expected,
        "lte": lambda: actual <= expected,
        "gt": lambda: actual > expected,
        "gte": lambda: actual >= expected,
    }[predicate.op]()


def estimate(
    snapshot: PricingSnapshot | dict | None, facts: PricingFacts | dict, profile: MetricProfile
) -> EstimateResult:
    """Unknown quantities never become zero; only estimated results carry a total."""
    if snapshot is None:
        return EstimateResult(status="unavailable", reason="snapshot_missing")
    try:
        snapshot = PricingSnapshot.model_validate(snapshot)
        facts = PricingFacts.model_validate(facts)
        if snapshot.profile != profile.id or facts.profile != profile.id:
            return EstimateResult(status="invalid", reason="profile_version_unsupported")
        if snapshot.reason or snapshot.rule is None:
            return EstimateResult(
                status="invalid" if snapshot.reason == "pricing_invalid" else "unavailable",
                reason=snapshot.reason or "price_missing",
            )
        rule = validate_rule(snapshot.rule, profile)
        validate_context(snapshot.context, profile, time_basis=rule.time_basis)
        validate_context(facts.context, profile, time_basis=rule.time_basis)
        if any(
            k in snapshot.context and snapshot.context[k] != v for k, v in facts.context.items()
        ):
            return EstimateResult(status="invalid", reason="meter_conflict")
        for key, fact in facts.values.items():
            if profile.quantities.get(key) != fact.unit:
                return EstimateResult(status="invalid", reason="meter_conflict")
            if key in profile.structural_zero and fact.value != 0:
                return EstimateResult(status="invalid", reason="meter_conflict")
        if facts.error:
            return EstimateResult(status="invalid", reason=facts.error)
        complete = {key: value.value for key, value in facts.values.items() if value.complete}
        # Validate subset relationships, including caller-supplied preview facts.
        for total, subsets in (
            ("input_total_tokens", ("input_uncached_tokens", "input_cache_read_tokens")),
            (
                "input_total_tokens",
                (
                    "input_regular_tokens",
                    "input_cache_read_tokens",
                    *(
                        ("input_cache_write_tokens",)
                        if "usage.input_cache_write_tokens" in profile.quantities
                        else ()
                    ),
                ),
            ),
            ("input_total_tokens", ("input_text_tokens", "input_image_tokens")),
            ("output_total_tokens", ("output_regular_tokens", "reasoning_tokens")),
            ("output_total_tokens", ("output_text_tokens", "output_image_tokens")),
        ):
            maximum = facts.values.get(f"usage.{total}")
            known = [facts.values.get(f"usage.{key}") for key in subsets]
            if maximum and sum(v.value for v in known if v) > maximum.value:
                return EstimateResult(status="invalid", reason="meter_conflict")
            if maximum and all(known) and sum(v.value for v in known) != maximum.value:
                return EstimateResult(status="invalid", reason="meter_conflict")
        success = facts.values.get("events.upstream_success_count")
        if success and success.value not in (0, 1):
            return EstimateResult(status="invalid", reason="meter_conflict")
        values = {
            **complete,
            **{f"context.{k}": v for k, v in {**snapshot.context, **facts.context}.items()},
        }
        detail = {
            "rule_hash": digest(rule.model_dump(mode="json", by_alias=True)),
            "facts_hash": digest(facts.model_dump(mode="json")),
            "items": [],
        }
        if rule.time_basis and rule.time_basis.calendar == "weekdays":
            weekday = values.get("context.pricing_weekday")
            if weekday is None:
                return EstimateResult(
                    status="partial",
                    currency=rule.currency,
                    reason="condition_missing",
                    detail={**detail, "missing": ["context.pricing_weekday"]},
                )
            if weekday >= 5:
                return EstimateResult(
                    status="unavailable",
                    currency=rule.currency,
                    reason="pricing_calendar_excluded",
                    detail=detail,
                )
        node = rule.root
        try:
            while isinstance(node, Branch):
                node = node.then if _predicate(node.when, values) else node.otherwise
        except MissingCondition as error:
            return EstimateResult(
                status="partial",
                currency=rule.currency,
                reason="condition_missing",
                detail={**detail, "missing": error.fields},
            )
        assert isinstance(node, Price)
        detail.update(
            leaf=node.id,
            factor=node.factor,
            time_basis=(rule.time_basis.model_dump() if rule.time_basis else None),
        )
        missing, total = [], Decimal(0)
        with localcontext() as context:
            context.prec = CALCULATION_PRECISION
            for item in node.items:
                quantity = complete.get(item.quantity)
                rate = Decimal(item.rate)
                amount = (
                    Decimal(quantity) * rate / item.per
                    if quantity is not None
                    else Decimal(0)
                    if rate == 0 or Decimal(node.factor) == 0
                    else None
                )
                if amount is None:
                    missing.append(item.quantity)
                else:
                    total += amount
                detail["items"].append(
                    {
                        "id": item.id,
                        "quantity": item.quantity,
                        "value": quantity,
                        "unit": profile.quantities[item.quantity],
                        "rate": item.rate,
                        "per": item.per,
                        "amount": format(amount, "f") if amount is not None else None,
                        "source": facts.values[item.quantity].source
                        if item.quantity in facts.values
                        else None,
                    }
                )
            if missing:
                return EstimateResult(
                    status="partial",
                    currency=rule.currency,
                    reason="metric_missing",
                    detail={**detail, "missing": missing},
                )
            total = (total * Decimal(node.factor)).quantize(
                Decimal("1e-18"), rounding=ROUND_HALF_EVEN
            )
            if not total.is_finite() or total >= Decimal("1e20"):
                return EstimateResult(status="invalid", reason="price_overflow", detail=detail)
        return EstimateResult(
            status="estimated", amount=format(total, "f"), currency=rule.currency, detail=detail
        )
    except DecimalException:
        return EstimateResult(status="invalid", reason="price_overflow")
    except (ValueError, TypeError, KeyError, RecursionError):
        return EstimateResult(status="invalid", reason="pricing_invalid")

from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from fractions import Fraction

import pytest
from support.pricing import SOURCE, image_rule, token_rule

from keystone.platform.model_access.adapters.inference.pricing import (
    PROFILES,
    merge_usage,
    normalize_pricing_facts,
    observed_time_context,
)
from keystone.platform.model_access.schemas.pricing import PriceRule, PricingFacts, PricingSnapshot
from keystone.platform.model_access.services.pricing_calculator import estimate, validate_rule

CHAT = PROFILES["chat.v1"]
BINDING = {
    "channel_id": "test",
    "provider": "newapi",
    "api_base": "https://example.com/v1",
    "provider_model": "test",
    "operation": "text.generate",
    "delivery": "sync",
}


def snapshot(rule, *, profile=CHAT, context=None):
    return PricingSnapshot(
        profile=profile.id,
        revision=1,
        binding=BINDING,
        rule=rule,
        source=SOURCE,
        context=context or {},
    )


def facts(profile=CHAT, **values):
    return PricingFacts(
        profile=profile.id,
        values={
            key: {"value": value, "unit": profile.quantities[key], "source": "manual"}
            for key, value in values.items()
        },
    )


def calculate(rule, values, *, profile=CHAT, context=None):
    return estimate(snapshot(rule, profile=profile, context=context), values, profile)


def test_cache_and_reasoning_do_not_double_charge_and_replay_is_stable():
    usage = {
        "prompt_tokens": 1000,
        "prompt_tokens_details": {"cached_tokens": 100},
        "completion_tokens": 200,
        "completion_tokens_details": {"reasoning_tokens": 150},
    }
    measured = normalize_pricing_facts(CHAT, usage, complete=True, success=True)
    first = calculate(token_rule(cached=True), measured)
    assert first.status == "estimated" and Decimal(first.amount) == Decimal("0.00342")
    assert first.detail["items"][0]["value"] == 900
    assert calculate(token_rule(cached=True), measured) == first
    assert usage["prompt_tokens"] == 1000  # Original budget usage remains inclusive.


@pytest.mark.parametrize("length,leaf,rate", [(32000, "short", "2"), (32001, "long", "4")])
def test_context_tier_uses_inclusive_input_not_regular_tokens(length, leaf, rate):
    short, long = token_rule(cached=True), token_rule(cached=True, input_rate="4")
    short["root"]["id"], long["root"]["id"] = "short", "long"
    # Cache has the same selected rate here to verify all tokens use the selected tier.
    long["root"]["items"][1]["rate"] = "4"
    short["root"]["items"][1]["rate"] = "2"
    rule = {
        "format": 1,
        "currency": "CNY",
        "root": {
            "kind": "branch",
            "when": {"field": "usage.input_total_tokens", "op": "lte", "value": 32000},
            "then": short["root"],
            "else": long["root"],
        },
    }
    values = facts(
        **{
            "usage.input_total_tokens": length,
            "usage.input_regular_tokens": 0,
            "usage.input_cache_read_tokens": length,
            "usage.output_total_tokens": 0,
        }
    )
    result = calculate(rule, values)
    assert result.detail["leaf"] == leaf
    assert Decimal(result.amount) == Decimal(length) * Decimal(rate) / 1000000


def test_missing_condition_never_falls_into_else_even_if_else_is_free():
    rule = token_rule()
    other = token_rule(factor="0")["root"]
    other["id"] = "free"
    rule["root"] = {
        "kind": "branch",
        "when": {"field": "usage.input_total_tokens", "op": "lte", "value": 32000},
        "then": rule["root"],
        "else": other,
    }
    result = calculate(rule, facts())
    assert (result.status, result.reason, result.amount) == ("partial", "condition_missing", None)


@pytest.mark.parametrize("factor,rate,expected", [("0", "2", "0"), ("1", "0", "0")])
def test_explicit_zero_is_known_without_unknown_quantity(factor, rate, expected):
    result = calculate(token_rule(input_rate=rate, output_rate=rate, factor=factor), facts())
    assert result.status == "estimated" and Decimal(result.amount) == Decimal(expected)


def test_missing_and_incomplete_metrics_are_partial_without_a_total():
    values = facts(**{"usage.input_total_tokens": 1000, "usage.output_total_tokens": 200})
    values.values["usage.output_total_tokens"].complete = False
    result = calculate(token_rule(), values)
    assert result.status == "partial" and result.amount is None
    assert result.detail["missing"] == ["usage.output_total_tokens"]
    assert result.detail["items"][0]["amount"] == "0.002"


@pytest.mark.parametrize("value", [True, -1, 2**63, "1", 1.0, float("nan")])
def test_usage_rejects_non_integer_or_out_of_range_numbers(value):
    invalid = {
        "profile": CHAT.id,
        "values": {
            "usage.input_total_tokens": {"value": value, "unit": "token", "source": "manual"}
        },
    }
    result = calculate(token_rule(), invalid)
    assert result.status == "invalid" and result.amount is None


@pytest.mark.parametrize(
    "rate",
    ["NaN", "Infinity", "-1", "1e3", "1.0000000000000000001", "100000000000000000000", " 1", 1],
)
def test_decimal_prices_are_bounded_strings(rate):
    with pytest.raises(ValueError):
        validate_rule(token_rule(input_rate=rate), CHAT)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda r: r["root"]["items"].append(deepcopy(r["root"]["items"][0])),
        lambda r: r["root"]["items"].append(
            {"id": "overlap", "quantity": "usage.input_cache_read_tokens", "rate": "0", "per": 1}
        ),
        lambda r: r["root"]["items"][0].update(quantity="usage.__class__"),
        lambda r: r["root"]["items"][0].update(quantity="__import__('os').system('whoami')"),
        lambda r: r["root"]["items"][0].update(per=0),
        lambda r: r["root"]["items"][0].update(per=True),
        lambda r: r["root"]["items"][0].update(per=10**12 + 1),
        lambda r: r["root"].update(factor="1000001"),
        lambda r: r.update(format=2),
        lambda r: r.update(currency="credits"),
        lambda r: r["root"].update(input_partition="modality_cache"),
        lambda r: r["root"].update(extra="arbitrary"),
    ],
)
def test_invalid_or_overlapping_rules_cannot_be_saved(mutation):
    rule = token_rule()
    mutation(rule)
    with pytest.raises(ValueError):
        validate_rule(rule, CHAT)


def test_every_nonzero_partition_component_requires_a_price():
    rule = token_rule(cached=True)
    rule["root"]["items"].pop(1)
    with pytest.raises(ValueError):
        validate_rule(rule, CHAT)


def test_rule_depth_and_size_are_bounded_before_recursive_validation():
    rule = token_rule()
    for index in range(9):
        other = token_rule()["root"]
        other["id"] = f"other_{index}"
        rule["root"] = {
            "kind": "branch",
            "when": {"field": "usage.input_total_tokens", "op": "eq", "value": 0},
            "then": rule["root"],
            "else": other,
        }
    with pytest.raises(ValueError):
        PriceRule.model_validate(rule)
    with pytest.raises(ValueError):
        PriceRule.model_validate({**token_rule(), "unexpected": "x" * 16384})


@pytest.mark.parametrize(
    "rate,amount",
    [
        ("0.000000000000000001", "0.000000000000000000"),
        ("0.000000000000000003", "0.000000000000000002"),
    ],
)
def test_rounds_total_once_using_half_even(rate, amount):
    rule = token_rule(input_rate=rate, output_rate="0")
    rule["root"]["items"][0]["per"] = 2
    assert calculate(rule, facts(**{"usage.input_total_tokens": 1})).amount == amount


def test_amount_overflow_is_invalid_not_zero_or_truncated():
    rule = token_rule(input_rate="99999999999999999999", output_rate="0")
    rule["root"]["items"][0]["per"] = 1
    result = calculate(rule, facts(**{"usage.input_total_tokens": 2}))
    assert (result.status, result.reason, result.amount) == ("invalid", "price_overflow", None)


@pytest.mark.parametrize(
    "profile_name", ["openai_images.v1", "openrouter_images.v1", "kling_images.v1"]
)
def test_image_quantity_and_request_event_are_independent_of_requested_n(profile_name):
    profile = PROFILES[profile_name]
    measured = normalize_pricing_facts(profile, {}, complete=True, success=True, generated=1)
    assert Decimal(calculate(image_rule(), measured, profile=profile).amount) == Decimal("0.2")
    assert Decimal(calculate(image_rule("3", request=True), measured, profile=profile).amount) == 3
    for unsupported in ("usage.billable_seconds", "usage.credits"):
        rule = image_rule("0")
        rule["root"]["items"][0]["quantity"] = unsupported
        with pytest.raises(ValueError):
            validate_rule(rule, profile)


def test_embeddings_total_tokens_are_input_and_output_is_structural_zero():
    profile = PROFILES["embedding.v1"]
    measured = normalize_pricing_facts(profile, {"total_tokens": 1000}, complete=True, success=True)
    result = calculate(token_rule(), measured, profile=profile)
    assert Decimal(result.amount) == Decimal("0.002")
    assert measured.values["events.upstream_success_count"].value == 1


def test_image_native_usage_modalities_and_missing_cache_intersection():
    profile = PROFILES["openai_images.v1"]
    measured = normalize_pricing_facts(
        profile,
        {
            "input_tokens": 100,
            "output_tokens": 200,
            "input_tokens_details": {"text_tokens": 20, "image_tokens": 80},
            "output_tokens_details": {"text_tokens": 0, "image_tokens": 200},
        },
        complete=True,
    )
    assert Decimal(calculate(token_rule(), measured, profile=profile).amount) == Decimal("0.0018")
    assert calculate(token_rule(cached=True), measured, profile=profile).reason == "metric_missing"
    measured.values["usage.input_image_tokens"].value = 101
    assert calculate(token_rule(), measured, profile=profile).reason == "meter_conflict"


def test_cumulative_sse_usage_replaces_fields_without_summing_or_copying_secrets():
    first = merge_usage({}, {"prompt_tokens": 100, "cost": "secret", "prompt": "private"})
    last = merge_usage(first, {"prompt_tokens": 100, "completion_tokens": 20})
    assert last == {"prompt_tokens": 100, "completion_tokens": 20}
    assert (
        calculate(token_rule(), normalize_pricing_facts(CHAT, last, complete=False)).status
        == "partial"
    )


def test_corrupt_cache_counts_never_make_negative_regular_tokens():
    measured = normalize_pricing_facts(
        CHAT, {"prompt_tokens": 10, "prompt_cache_hit_tokens": 11}, complete=True
    )
    assert calculate(token_rule(cached=True), measured).reason == "meter_conflict"


def test_relay_cache_writes_are_separate_and_never_assumed_zero():
    profile = PROFILES["relay_chat.v1"]
    usage = {
        "prompt_tokens": 1000,
        "completion_tokens": 200,
        "prompt_tokens_details": {"cached_tokens": 100, "cache_write_tokens": 50},
    }
    measured = normalize_pricing_facts(profile, usage, complete=True)
    result = calculate(token_rule(cached=True, writes=True), measured, profile=profile)
    assert Decimal(result.amount) == Decimal("0.003445")
    assert measured.values["usage.input_regular_tokens"].value == 850
    del usage["prompt_tokens_details"]["cache_write_tokens"]
    measured = normalize_pricing_facts(profile, usage, complete=True)
    assert (
        calculate(token_rule(cached=True, writes=True), measured, profile=profile).status
        == "partial"
    )


def test_unsupported_parameter_values_and_fake_structural_output_are_invalid():
    profile = PROFILES["openai_images.v1"]
    assert (
        calculate(
            image_rule(),
            facts(profile, **{"usage.generated_images": 1}),
            profile=profile,
            context={"quality": "ultra"},
        ).status
        == "invalid"
    )
    embedding = PROFILES["embedding.v1"]
    assert (
        calculate(
            token_rule(),
            facts(embedding, **{"usage.input_total_tokens": 10, "usage.output_total_tokens": 1}),
            profile=embedding,
        ).reason
        == "meter_conflict"
    )


def test_time_conditions_require_the_declared_source_not_a_poll_or_http_date():
    rule = token_rule()
    rule["time_basis"] = {
        "source": "observed_response",
        "timezone": "Asia/Shanghai",
        "calendar": "weekdays",
    }
    other = token_rule(factor="0")["root"]
    other["id"] = "night"
    rule["root"] = {
        "kind": "branch",
        "when": {"field": "context.pricing_hour", "op": "lte", "value": 16},
        "then": rule["root"],
        "else": other,
    }
    parsed = validate_rule(rule, CHAT)
    measured = facts(**{"usage.input_total_tokens": 100, "usage.output_total_tokens": 0})
    assert calculate(parsed, measured).reason == "condition_missing"
    measured.context = observed_time_context(parsed, datetime(2026, 10, 30, 8, tzinfo=UTC))
    assert calculate(parsed, measured).detail["leaf"] == "standard"
    parsed.time_basis.source = "upstream_completed"
    assert observed_time_context(parsed, datetime.now(UTC)) == {}


def test_legacy_and_unsupported_snapshot_versions_are_explicit():
    assert estimate(None, facts(), CHAT).reason == "legacy_no_snapshot"
    saved = snapshot(token_rule()).model_dump(mode="json", by_alias=True)
    saved["format"] = 2
    assert estimate(saved, facts(), CHAT).status == "invalid"
    saved["format"], saved["profile"] = 1, "chat.v999"
    assert estimate(saved, facts(), CHAT).reason == "profile_version_unsupported"


def test_weekday_calendar_never_prices_an_excluded_day_as_free():
    rule = token_rule()
    rule["time_basis"] = {"source": "upstream_completed", "timezone": "UTC", "calendar": "weekdays"}
    measured = facts(**{"usage.input_total_tokens": 100, "usage.output_total_tokens": 0})
    assert calculate(rule, measured).reason == "condition_missing"
    measured.context = {"pricing_weekday": 5}
    result = calculate(rule, measured)
    assert result.status == "unavailable" and result.reason == "pricing_calendar_excluded"
    assert result.amount is None


def test_explicit_per_units_and_fact_unit_mismatches():
    profile = PROFILES["kling_images.v1"]
    rule = image_rule("2")
    rule["root"]["items"][0]["per"] = 1000
    measured = facts(profile, **{"usage.generated_images": 1})
    assert Decimal(calculate(rule, measured, profile=profile).amount) == Decimal("0.002")
    measured.values["usage.generated_images"].unit = "token"
    assert calculate(rule, measured, profile=profile).reason == "meter_conflict"


@pytest.mark.parametrize("version", [True, 1.0, "1", 2])
def test_format_version_is_an_integer_and_a_price_requires_provenance(version):
    saved = snapshot(token_rule()).model_dump(mode="json", by_alias=True)
    saved["format"] = version
    assert estimate(saved, facts(), CHAT).status == "invalid"
    saved["format"], saved["source"] = 1, None
    assert estimate(saved, facts(), CHAT).status == "invalid"
    with pytest.raises(ValueError):
        PricingFacts.model_validate({"format": version, "profile": CHAT.id})


@pytest.mark.parametrize("factor", ["1", "0.000000000000000001", "999999.999999999999999999"])
def test_rational_prices_round_like_an_independent_exact_fraction(factor):
    profile = PROFILES["openai_images.v1"]
    quantities = [
        "usage.input_text_tokens",
        "usage.input_image_tokens",
        "usage.output_text_tokens",
        "usage.output_image_tokens",
        "usage.generated_images",
        "events.upstream_success_count",
    ]
    counts = [10, 9223372036854775797, 10, 90, 1, 1]
    denominators = [999999999989, 999999999959, 999999999937, 999999999929, 7, 3]
    rule = {
        "format": 1,
        "currency": "CNY",
        "root": {
            "kind": "price",
            "id": "rational",
            "input_partition": "modality",
            "output_partition": "modality",
            "factor": factor,
            "items": [
                {"id": f"item_{index}", "quantity": key, "rate": "1.123456789123456789", "per": per}
                for index, (key, per) in enumerate(zip(quantities, denominators, strict=True))
            ],
        },
    }
    measured = facts(profile, **dict(zip(quantities, counts, strict=True)))
    exact = (
        sum(
            Fraction(count) * Fraction("1.123456789123456789") / per
            for count, per in zip(counts, denominators, strict=True)
        )
        * Fraction(factor)
        * 10**18
    )
    whole, remainder = divmod(exact.numerator, exact.denominator)
    whole += int(
        2 * remainder > exact.denominator or 2 * remainder == exact.denominator and whole % 2
    )
    expected = f"{whole // 10**18}.{whole % 10**18:018d}"
    assert calculate(rule, measured, profile=profile).amount == expected

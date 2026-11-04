"""Provider-shaped receipts and verified catalog prices, without paid requests."""

from decimal import Decimal

import httpx2
import pytest
from support.pricing import SOURCE, token_rule

from keystone.platform.model_access.adapters.channels import CHANNELS
from keystone.platform.model_access.adapters.inference.pricing import (
    PROFILES,
    merge_usage,
    normalize_pricing_facts,
    profile_for,
    request_context,
)
from keystone.platform.model_access.adapters.pricing.catalog import (
    ALI_BASE,
    ARK_BASE,
    REFERENCE_RULES,
    reference,
)
from keystone.platform.model_access.schemas.pricing import PricingSnapshot
from keystone.platform.model_access.services.pricing_calculator import estimate, validate_rule


def binding(provider="aliyun_bailian", model="qwen-plus", operation="text.generate"):
    return {
        "channel_id": "test",
        "provider": provider,
        "api_base": ALI_BASE if provider == "aliyun_bailian" else ARK_BASE,
        "provider_model": model,
        "operation": operation,
        "delivery": "sync",
    }


def calculate(usage, rule=None, *, profile="relay_chat.v2", context=None):
    meter = PROFILES[profile]
    facts = normalize_pricing_facts(meter, merge_usage({}, usage), complete=True)
    snapshot = PricingSnapshot(
        profile=profile,
        binding=binding(),
        revision=0,
        rule=rule or token_rule(),
        source=SOURCE,
        context=context or {},
    )
    return estimate(snapshot, facts, meter), facts


def read_rule():
    rule = token_rule(cached=True)
    rule["root"]["input_partition"] = "cache_read"
    rule["root"]["items"][0]["quantity"] = "usage.input_uncached_tokens"
    return rule


@pytest.mark.parametrize(
    "profile",
    ["chat.v2", "relay_chat.v2", "volcengine_chat.v2", "openai_images.v2", "openrouter_images.v2"],
)
def test_common_cache_read_prices_do_not_require_a_fictitious_write_zero(profile):
    result, facts = calculate(
        {
            "input_tokens": 1000,
            "output_tokens": 200,
            "total_tokens": 1200,
            "input_tokens_details": {"cached_tokens": 100},
        },
        read_rule(),
        profile=profile,
    )
    assert result.status == "estimated" and Decimal(result.amount) == Decimal("0.00342")
    assert facts.values["usage.input_uncached_tokens"].value == 900
    assert "usage.input_cache_write_tokens" not in facts.values


def test_full_cache_partition_still_requires_actual_write_counts():
    result, _ = calculate(
        {
            "prompt_tokens": 1000,
            "completion_tokens": 200,
            "prompt_tokens_details": {"cached_tokens": 100},
        },
        token_rule(cached=True, writes=True),
    )
    assert result.status == "partial" and result.amount is None


@pytest.mark.parametrize(
    "receipt",
    [
        {"prompt_tokens": 1000, "prompt_cache_hit_tokens": 100, "prompt_cache_miss_tokens": 900},
        {"prompt_cache_hit_tokens": 100, "prompt_cache_miss_tokens": 900},
        {"prompt_tokens": 1000, "prompt_cache_miss_tokens": 900},
    ],
)
def test_deepseek_compatible_hit_miss_partition_is_used_and_can_derive_the_missing_counter(receipt):
    result, facts = calculate({**receipt, "completion_tokens": 200}, read_rule())
    assert Decimal(result.amount) == Decimal("0.00342")
    assert facts.values["usage.input_total_tokens"].value == 1000
    assert facts.values["usage.input_cache_read_tokens"].value == 100


def test_cache_miss_includes_independently_reported_writes_without_double_charging():
    result, facts = calculate(
        {
            "prompt_tokens": 1000,
            "completion_tokens": 200,
            "prompt_cache_hit_tokens": 100,
            "prompt_cache_miss_tokens": 900,
            "prompt_tokens_details": {"cached_tokens": 100, "cache_creation_input_tokens": 50},
        },
        token_rule(cached=True, writes=True),
    )
    assert Decimal(result.amount) == Decimal("0.003445")
    assert facts.values["usage.input_regular_tokens"].value == 850


@pytest.mark.parametrize(
    "patch",
    [
        {"input_tokens": 999},
        {"prompt_cache_hit_tokens": 101},
        {"prompt_cache_miss_tokens": 901},
        {"input_tokens_details": {"cached_tokens": 101}},
        {"completion_tokens_details": {"reasoning_tokens": 10}, "reasoning_tokens": 11},
        {
            "prompt_tokens_details": {
                "cached_tokens": 100,
                "cache_write_tokens": 20,
                "cache_creation_input_tokens": 21,
            }
        },
        {"prompt_tokens_details": {"cached_tokens": -1}},
        {"prompt_tokens_details": {"cached_tokens": True}},
        {"completion_tokens_details": {"audio_tokens": 1}},
        {"cache_read_input_tokens": 100},
    ],
)
def test_conflicting_aliases_and_uninstalled_audio_or_native_anthropic_usage_are_not_priced(patch):
    result, _ = calculate(
        {
            "prompt_tokens": 1000,
            "completion_tokens": 200,
            "prompt_tokens_details": {"cached_tokens": 100},
            **patch,
        }
    )
    assert result.status == "invalid" and result.amount is None


def test_nullable_optional_breakdowns_are_absent_not_invalid_or_zero():
    result, facts = calculate(
        {
            "prompt_tokens": 1000,
            "completion_tokens": 200,
            "prompt_tokens_details": None,
            "completion_tokens_details": {"reasoning_tokens": None, "audio_tokens": None},
        }
    )
    assert Decimal(result.amount) == Decimal("0.0036")
    assert "usage.reasoning_tokens" not in facts.values
    assert (
        calculate({"prompt_tokens": 1000, "completion_tokens": 200}, read_rule())[0].status
        == "partial"
    )
    assert (
        merge_usage(
            {"prompt_tokens_details": {"cached_tokens": 100}},
            {"prompt_tokens_details": None},
        )
        == {}
    )


def test_embeddings_never_hide_a_nonzero_reported_output_under_a_structural_zero():
    result, _ = calculate({"input_tokens": 1000, "total_tokens": 1000}, profile="embedding.v2")
    assert Decimal(result.amount) == Decimal("0.002")
    assert (
        calculate(
            {"prompt_tokens": 1000, "completion_tokens": 1, "total_tokens": 1001},
            profile="embedding.v2",
        )[0].status
        == "invalid"
    )


@pytest.mark.parametrize("target,price", list(REFERENCE_RULES.items()))
def test_every_reference_rule_is_compatible_with_its_explicitly_installed_operation(target, price):
    provider, _, model, operation, delivery = target
    protocol = CHANNELS[provider].protocol(operation, delivery, model=model)
    assert protocol is not None
    validate_rule(price.rule, profile_for(protocol))


@pytest.mark.parametrize(
    "provider,model,count,hit,expected",
    [
        ("aliyun_bailian", "qwen-plus", 128000, 1000, "0.1018"),
        ("aliyun_bailian", "qwen-plus", 128001, 1000, "0.3056824"),
        ("aliyun_bailian", "qwen-plus", 256001, 1000, "1.2259248"),
        ("aliyun_bailian", "qwen-flash", 128000, 0, "0.01923"),
        ("aliyun_bailian", "qwen3.7-flash", 32000, 0, "0.006416"),
        ("aliyun_bailian", "qwen3.7-flash", 32001, 0, "0.0192486"),
        ("volcengine", "doubao-seed-2-0-pro-260215", 32768, 1000, "0.1026176"),
        ("volcengine", "doubao-seed-2-0-pro-260215", 32769, 1000, "0.1539312"),
        ("volcengine", "doubao-seed-2-0-pro-260215", 32001, 1000, "0.1001632"),
        ("volcengine", "doubao-seed-2-0-pro-260215", 131072, 0, "0.6296256"),
        ("volcengine", "doubao-seed-2-0-pro-260215", 131073, 0, "1.2592608"),
        ("volcengine", "doubao-seed-2-1-lite-260915", 1000, 100, "0.00079"),
    ],
)
def test_reference_tiers_use_full_input_and_one_rate_for_the_whole_request(
    provider, model, count, hit, expected
):
    target = binding(provider, model)
    price = reference(target)
    result, _ = calculate(
        {
            "prompt_tokens": count,
            "completion_tokens": 20,
            "prompt_tokens_details": {"cached_tokens": hit},
        },
        price.rule,
        profile="chat.v2",
        context={"enable_thinking": 0},
    )
    assert result.status == "estimated" and Decimal(result.amount) == Decimal(expected)


def test_thinking_and_request_variants_cannot_silently_inherit_the_wrong_price():
    target = binding()
    price = reference(target)
    usage = {
        "prompt_tokens": 1000,
        "completion_tokens": 200,
        "prompt_tokens_details": {"cached_tokens": 0},
    }
    assert Decimal(
        calculate(usage, price.rule, profile="chat.v2", context={"enable_thinking": 1})[0].amount
    ) == Decimal("0.0024")
    assert calculate(usage, price.rule, profile="chat.v2")[0].status == "partial"
    assert reference(target, context={"cache_mode": "explicit"}) is None
    ark = binding("volcengine", "doubao-seed-2-1-pro-260915")
    assert reference(ark, context={"service_tier": "fast"}) is None
    assert reference(ark, context={"service_tier": "default"}) is not None
    assert reference({**ark, "provider_model": "ep-user-owned-alias"}) is None
    assert reference({**target, "provider": "newapi"}) is None
    assert (
        reference({**target, "api_base": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"})
        is None
    )
    request = httpx2.Request("POST", ARK_BASE + "/chat/completions", json={"messages": []})
    assert request_context(request, PROFILES["volcengine_chat.v2"])["service_tier"] == "default"


def test_effective_context_cannot_be_spoofed_by_synthetic_top_level_parameters():
    request = httpx2.Request(
        "POST",
        ALI_BASE + "/chat/completions",
        json={
            "cache_mode": "implicit",
            "cache_ttl": "1h",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "private",
                            "cache_control": {"type": "ephemeral", "ttl": "5m"},
                        }
                    ],
                }
            ],
        },
    )
    context = request_context(request, PROFILES["relay_chat.v2"])
    assert context == {"cache_mode": "explicit", "cache_ttl": "5m"}
    assert "private" not in str(context)
    request = httpx2.Request("POST", ARK_BASE + "/chat/completions", json={"service_tier": 42})
    context = request_context(request, PROFILES["volcengine_chat.v2"])
    assert "service_tier" not in context
    assert reference(binding("volcengine", "doubao-seed-evolving"), context=context) is None

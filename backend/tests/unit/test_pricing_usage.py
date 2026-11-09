"""Provider-shaped receipts and verified catalog prices, without paid requests."""

from decimal import Decimal

import httpx2
import pytest
from support.pricing import SOURCE, token_rule, ttl_rule

from keystone.platform.model_access.adapters.inference.pricing import (
    PROFILES,
    merge_usage,
    normalize_pricing_facts,
    profile_for,
    request_context,
)
from keystone.platform.model_access.adapters.inference.transport import usage as receipt_usage
from keystone.platform.model_access.channels import CHANNELS, reference
from keystone.platform.model_access.channels import REFERENCE_PRICES as REFERENCE_RULES
from keystone.platform.model_access.channels.aliyun_bailian.pricing import BASE as ALI_BASE
from keystone.platform.model_access.channels.volcengine.pricing import BASE as ARK_BASE
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


def installed(provider, model, operation="text.generate"):
    """The exact meter the channel installs for this model, never a guessed version."""
    return profile_for(CHANNELS[provider].protocol(operation, "sync", model=model))


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
    [
        "chat.v2",
        "chat.v3",
        "relay_chat.v2",
        "relay_chat.v3",
        "volcengine_chat.v2",
        "openai_images.v2",
        "openrouter_images.v2",
    ],
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
        profile=installed(provider, model).id,
        context={"enable_thinking": 0, "cache_mode": "implicit"},
    )
    assert result.status == "estimated" and Decimal(result.amount) == Decimal(expected)


def test_thinking_and_request_variants_cannot_silently_inherit_the_wrong_price():
    target = binding()
    price = reference(target)
    meter = installed("aliyun_bailian", "qwen-plus").id
    usage = {
        "prompt_tokens": 1000,
        "completion_tokens": 200,
        "prompt_tokens_details": {"cached_tokens": 0},
        "completion_tokens_details": {"reasoning_tokens": 100},
    }
    assert Decimal(
        calculate(
            usage,
            price.rule,
            profile=meter,
            context={"enable_thinking": 1, "cache_mode": "implicit"},
        )[0].amount
    ) == Decimal("0.0024")
    usage["completion_tokens_details"]["reasoning_tokens"] = 0
    assert Decimal(
        calculate(
            usage,
            price.rule,
            profile=meter,
            context={"enable_thinking": 1, "cache_mode": "implicit"},
        )[0].amount
    ) == Decimal("0.0012")
    del usage["completion_tokens_details"]
    assert (
        calculate(
            usage,
            price.rule,
            profile=meter,
            context={"enable_thinking": 1, "cache_mode": "implicit"},
        )[0].status
        == "partial"
    )
    assert (
        calculate(usage, price.rule, profile=meter, context={"cache_mode": "implicit"})[0].status
        == "partial"
    )
    # Explicit cache is a separate branch of the same published tariff: creating 1000
    # tokens bills 125% of the 0.8 input price and no ordinary input remains.
    explicit = reference(target, context={"cache_mode": "explicit"})
    assert explicit is not None
    created = calculate(
        {
            "prompt_tokens": 1000,
            "completion_tokens": 200,
            "prompt_tokens_details": {
                "cached_tokens": 0,
                "cache_creation_input_tokens": 1000,
            },
        },
        explicit.rule,
        profile=meter,
        context={"enable_thinking": 0, "cache_mode": "explicit"},
    )[0]
    assert created.status == "estimated" and Decimal(created.amount) == Decimal("0.0014")
    # Explicit reads bill 10% instead of the implicit 20%.
    hit = calculate(
        {
            "prompt_tokens": 1000,
            "completion_tokens": 0,
            "prompt_tokens_details": {"cached_tokens": 1000, "cache_creation_input_tokens": 0},
        },
        explicit.rule,
        profile=meter,
        context={"enable_thinking": 0, "cache_mode": "explicit"},
    )[0]
    assert hit.status == "estimated" and Decimal(hit.amount) == Decimal("0.00008")
    assert Decimal(
        calculate(
            {
                "prompt_tokens": 1000,
                "completion_tokens": 0,
                "prompt_tokens_details": {"cached_tokens": 1000},
            },
            explicit.rule,
            profile=meter,
            context={"enable_thinking": 0, "cache_mode": "implicit"},
        )[0].amount
    ) == Decimal("0.00016")
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


def test_stored_usage_keeps_cache_writes_and_only_reported_lifetimes():
    assert receipt_usage(
        {
            "prompt_tokens": 1000,
            "completion_tokens": 20,
            "prompt_tokens_details": {
                "cached_tokens": 100,
                "cache_creation_input_tokens": 50,
            },
            "claude_cache_creation_5_m_tokens": 30,
        }
    ) == {
        "input_tokens": 1000,
        "output_tokens": 20,
        "cache_read_input_tokens": 100,
        "cache_write_input_tokens": 50,
        "cache_write_5m_input_tokens": 30,
    }
    # Statistics never sum aliases of the same tokens, and never invent a zero.
    assert (
        receipt_usage(
            {
                "prompt_tokens": 1,
                "prompt_tokens_details": {
                    "cache_write_tokens": 50,
                    "cached_creation_tokens": 70,
                },
            }
        )["cache_write_input_tokens"]
        == 70
    )
    assert receipt_usage({"prompt_tokens": 10}) == {"input_tokens": 10}


def test_installed_chat_meters_now_declare_cache_writes():
    for name in ("chat.v3", "relay_chat.v3"):
        profile = PROFILES[name]
        assert "usage.input_cache_write_tokens" in profile.input_partitions["cache"]
        assert "usage.input_cache_write_5m_tokens" in profile.input_partitions["cache_ttl"]
        # The requested lifetime stays a request fact; writes come from the response.
        assert profile.context_fields["context.cache_ttl"] == "string"
    # Frozen v2 stays resolvable for snapshots registered before writes were priced.
    assert "usage.input_cache_write_tokens" in PROFILES["relay_chat.v2"].quantities
    assert "usage.input_cache_write_5m_tokens" not in PROFILES["relay_chat.v2"].quantities


def test_cache_write_aliases_price_the_same_tokens_once():
    result, facts = calculate(
        {
            "prompt_tokens": 1000,
            "completion_tokens": 200,
            "prompt_tokens_details": {
                "cached_tokens": 100,
                "cached_creation_tokens": 50,
            },
        },
        token_rule(cached=True, writes=True),
        profile="relay_chat.v3",
    )
    assert Decimal(result.amount) == Decimal("0.003445")
    assert facts.values["usage.input_regular_tokens"].value == 850


@pytest.mark.parametrize(
    "writes",
    [
        {"cache_write_tokens": 50, "cached_creation_tokens": 51},
        {"cache_write_tokens": 50, "cache_creation_input_tokens": 51},
    ],
)
def test_disagreeing_write_aliases_are_a_conflict_not_a_maximum(writes):
    result, _ = calculate(
        {
            "prompt_tokens": 1000,
            "completion_tokens": 200,
            "prompt_tokens_details": {"cached_tokens": 100, **writes},
        },
        token_rule(cached=True, writes=True),
        profile="relay_chat.v3",
    )
    assert result.status == "invalid" and result.amount is None


def test_write_lifetimes_price_disjoint_components():
    result, facts = calculate(
        {
            "prompt_tokens": 1000,
            "completion_tokens": 200,
            "prompt_tokens_details": {"cached_tokens": 100, "cached_creation_tokens": 100},
            "claude_cache_creation_5_m_tokens": 60,
            "claude_cache_creation_1_h_tokens": 40,
        },
        ttl_rule(),
        profile="relay_chat.v3",
    )
    # 800 regular + 100 read + 60 x 5m + 40 x 1h + 200 output, each billed once.
    assert result.status == "estimated" and Decimal(result.amount) == Decimal("0.00353")
    assert facts.values["usage.input_regular_tokens"].value == 800
    assert facts.values["usage.input_cache_write_5m_tokens"].value == 60


def test_write_lifetimes_must_remain_subsets_of_the_reported_write_count():
    result, _ = calculate(
        {
            "prompt_tokens": 1000,
            "completion_tokens": 200,
            "prompt_tokens_details": {"cached_tokens": 100, "cached_creation_tokens": 100},
            "claude_cache_creation_5_m_tokens": 60,
            "claude_cache_creation_1_h_tokens": 50,
        },
        ttl_rule(),
        profile="relay_chat.v3",
    )
    assert result.status == "invalid" and result.reason == "meter_conflict"


@pytest.mark.parametrize(
    "details",
    [
        {"cache_write_tokens_details": {"ttl_12h_tokens": 10}},
        {"cache_write_tokens_details": {"ttl_30m_tokens": 10}},
        {"cache_write_tokens_details": "unknown"},
    ],
)
def test_unverified_write_lifetimes_are_not_priced_as_the_ordinary_write_rate(details):
    result, _ = calculate(
        {
            "prompt_tokens": 1000,
            "completion_tokens": 200,
            "prompt_tokens_details": {
                "cached_tokens": 100,
                "cache_write_tokens": 10,
                **details,
            },
        },
        token_rule(cached=True, writes=True),
        profile="relay_chat.v3",
    )
    assert result.status == "invalid" and result.reason == "meter_conflict"

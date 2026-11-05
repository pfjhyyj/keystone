"""Verified reference prices, never relay prices or a live account tariff lookup.

Only exact public model IDs are listed. Endpoint aliases and unlisted models require
an administrator override; absence is deliberately not a zero price.
"""

from copy import deepcopy
from decimal import Decimal

from keystone.platform.model_access.schemas.pricing import OperationPricing

ALI_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"
ARK_BASE = "https://ark.cn-beijing.volces.com/api/v3"
SOURCE = {
    "label": "百炼北京地域按量原价；不抵扣免费额度、资源包或优惠",
    "url": "https://help.aliyun.com/zh/model-studio/model-pricing",
    "version": "2026-10-30.2",
    "checked_at": "2026-10-30T00:00:00Z",
}

ARK_SOURCE = {
    **SOURCE,
    "label": "方舟北京地域在线标准服务原价；不含缓存存储、资源包或优惠",
    "url": "https://docs.volcengine.com/docs/ark/model-pricing?lang=zh",
}
ALI_CACHE_SOURCE = {
    **SOURCE,
    "version": "2026-11-05.1",
    "label": "百炼北京实时推理原价及隐式缓存命中价；不含显式缓存、免费额度或限时折扣",
}


def token_rule(input_rate, output_rate=None):
    return {
        "format": 1,
        "currency": "CNY",
        "root": {
            "kind": "price",
            "id": "standard",
            "input_partition": "aggregate",
            "output_partition": "aggregate" if output_rate is not None else "none",
            "factor": "1",
            "items": [
                {
                    "id": "input",
                    "quantity": "usage.input_total_tokens",
                    "rate": input_rate,
                    "per": 1000000,
                },
                *(
                    [
                        {
                            "id": "output",
                            "quantity": "usage.output_total_tokens",
                            "rate": output_rate,
                            "per": 1000000,
                        }
                    ]
                    if output_rate is not None
                    else []
                ),
            ],
        },
    }


REFERENCE_RULES = {
    **{
        ("aliyun_bailian", ALI_BASE, "qwen-max", operation, "sync"): OperationPricing(
            operation=operation, rule=token_rule("2.4", "9.6"), source=SOURCE
        )
        for operation in ("text.generate", "text.structured")
    },
    **{
        ("aliyun_bailian", ALI_BASE, model, "text.embed", "sync"): OperationPricing(
            operation="text.embed", rule=token_rule(rate), source=SOURCE
        )
        for model, rate in (
            ("text-embedding-v4", "0.5"),
            ("text-embedding-v3", "0.5"),
            ("text-embedding-v2", "0.7"),
            ("text-embedding-v1", "0.7"),
            ("qwen3.7-text-embedding", "0.5"),
            ("qwen3.7-text-embedding-flash", "0.125"),
        )
    },
}


def tier_rule(rows):
    """Whole-request tiers with exact token boundaries; writes stay at ordinary input price."""
    leaves = []
    for index, (_, incoming, outgoing, cached) in enumerate(rows):
        leaf = token_rule(incoming, outgoing)["root"]
        leaf["id"] = f"tier_{index}"
        leaf["input_partition"] = "cache_read"
        leaf["items"][0]["quantity"] = "usage.input_uncached_tokens"
        leaf["items"].append(
            {
                "id": "cache_read",
                "quantity": "usage.input_cache_read_tokens",
                "rate": cached,
                "per": 1000000,
            }
        )
        leaves.append(leaf)
    root = leaves[-1]
    for (threshold, *_), leaf in reversed(list(zip(rows[:-1], leaves[:-1], strict=True))):
        root = {
            "kind": "branch",
            "when": {"field": "usage.input_total_tokens", "op": "lte", "value": threshold},
            "then": leaf,
            "else": root,
        }
    return {"format": 1, "currency": "CNY", "root": root}


def ali_tiers(rows):
    # Official implicit-cache tariff: 20% for these explicitly enumerated families.
    # Newer Qwen 3.8 exceptions require a console quote; never infer their cache price.
    return tier_rule(
        [
            (limit, incoming, outgoing, format(Decimal(incoming) * Decimal("0.2"), "f"))
            for limit, incoming, outgoing in rows
        ]
    )


ALI_CHAT_RULES = {
    **dict.fromkeys(
        ("qwen3.7-max", "qwen3.7-max-2026-05-20", "qwen3.7-max-2026-06-08"),
        ali_tiers([(1000000, "12", "36")]),
    ),
    **dict.fromkeys(
        ("qwen3.7-plus", "qwen3.7-plus-2026-05-26"),
        ali_tiers([(256000, "2", "8"), (1000000, "6", "24")]),
    ),
    "qwen3.6-plus": ali_tiers([(256000, "2", "12"), (1000000, "8", "48")]),
    **dict.fromkeys(
        ("qwen3.5-plus", "qwen3.5-plus-2026-04-20"),
        ali_tiers([(128000, "0.8", "4.8"), (256000, "2", "12"), (1000000, "4", "24")]),
    ),
    **dict.fromkeys(
        ("qwen3.7-flash", "qwen3.7-flash-2026-07-15"),
        ali_tiers([(32000, "0.2", "0.8"), (256000, "0.6", "2.4"), (1000000, "1.2", "4.8")]),
    ),
    "qwen3.6-flash": ali_tiers([(256000, "1.2", "7.2"), (1000000, "4.8", "28.8")]),
    "qwen3.5-flash": ali_tiers(
        [(128000, "0.2", "2"), (256000, "0.8", "8"), (1000000, "1.2", "12")]
    ),
    "qwen-flash": ali_tiers(
        [(128000, "0.15", "1.5"), (256000, "0.6", "6"), (1000000, "1.2", "12")]
    ),
    "qwen-plus": {
        "format": 1,
        "currency": "CNY",
        "root": {
            "kind": "branch",
            "when": {"field": "context.enable_thinking", "op": "eq", "value": 0},
            "then": ali_tiers(
                [(128000, "0.8", "2"), (256000, "2.4", "20"), (1000000, "4.8", "48")]
            )["root"],
            "else": {
                **ali_tiers([(128000, "0.8", "8"), (256000, "2.4", "24"), (1000000, "4.8", "64")])[
                    "root"
                ]
            },
        },
    },
}


# Leaf IDs must remain unique across both thinking branches.
def _prefix_leaves(node, prefix):
    if node["kind"] == "price":
        node["id"] = prefix + node["id"]
    else:
        _prefix_leaves(node["then"], prefix)
        _prefix_leaves(node["else"], prefix)


_prefix_leaves(ALI_CHAT_RULES["qwen-plus"]["root"]["else"], "thinking_")
# A thinking request that reports zero reasoning uses the non-thinking output tariff.
# Missing reasoning usage remains unknown, rather than assuming the request took effect.
_qwen_price = ALI_CHAT_RULES["qwen-plus"]["root"]
_no_reasoning = deepcopy(_qwen_price["then"])
_prefix_leaves(_no_reasoning, "no_reasoning_")
_qwen_price["else"] = {
    "kind": "branch",
    "when": {"field": "usage.reasoning_tokens", "op": "gt", "value": 0},
    "then": _qwen_price["else"],
    "else": _no_reasoning,
}


# Published aliases/snapshots with the same prices; no substring or date inference.
for alias, target in {
    "qwen-plus-latest": "qwen-plus",
    "qwen-plus-2025-12-01": "qwen-plus",
    "qwen3.6-plus-2026-04-02": "qwen3.6-plus",
    "qwen3.5-plus-2026-02-15": "qwen3.5-plus",
    "qwen3.6-flash-2026-04-16": "qwen3.6-flash",
    "qwen3.5-flash-2026-02-23": "qwen3.5-flash",
    "qwen-flash-2025-07-28": "qwen-flash",
}.items():
    ALI_CHAT_RULES[alias] = ALI_CHAT_RULES[target]

# API model IDs, not console display names or user-owned ep-* aliases. Ark's
# charge-item bounds use 32K=32768 (GetModelActivation.MultiChargeItems), unlike Ali.
ARK_CHAT_RULES = {
    **dict.fromkeys(
        ("doubao-seed-evolving", "doubao-seed-2-1-pro-260628", "doubao-seed-2-1-pro-260915"),
        tier_rule([(1048576, "6", "30", "1.2")]),
    ),
    "doubao-seed-2-1-lite-260915": tier_rule([(1048576, "0.8", "2.7", "0.16")]),
    "doubao-seed-2-1-turbo-260628": tier_rule([(262144, "3", "15", "0.6")]),
    "doubao-seed-2-0-pro-260215": tier_rule(
        [(32768, "3.2", "16", "0.64"), (131072, "4.8", "24", "0.96"), (262144, "9.6", "48", "1.92")]
    ),
    **dict.fromkeys(
        ("doubao-seed-2-0-lite-260215", "doubao-seed-2-0-lite-260428"),
        tier_rule(
            [
                (32768, "0.6", "3.6", "0.12"),
                (131072, "0.9", "5.4", "0.18"),
                (262144, "1.8", "10.8", "0.36"),
            ]
        ),
    ),
    "doubao-seed-2-0-mini-260215": tier_rule(
        [(32768, "0.2", "2", "0.04"), (131072, "0.4", "4", "0.08"), (262144, "0.8", "8", "0.16")]
    ),
    "doubao-seed-2-0-code-preview-260215": tier_rule(
        [(32768, "3.2", "16", "0.64"), (131072, "4.8", "24", "0.96"), (262144, "9.6", "48", "1.92")]
    ),
}
REFERENCE_RULES.update(
    {
        (provider, base, model, operation, "sync"): OperationPricing(
            operation=operation, rule=rule, source=source
        )
        for provider, base, rules, source in (
            ("aliyun_bailian", ALI_BASE, ALI_CHAT_RULES, ALI_CACHE_SOURCE),
            ("volcengine", ARK_BASE, ARK_CHAT_RULES, ARK_SOURCE),
        )
        for model, rule in rules.items()
        for operation in ("text.generate", "text.structured")
    }
)


def reference(binding, *, context=None):
    if context is not None:
        if binding["provider"] == "volcengine" and context.get("service_tier") != "default":
            return None
        if binding["provider"] == "aliyun_bailian" and context.get("cache_mode") == "explicit":
            return None
    return REFERENCE_RULES.get(
        tuple(
            binding[key]
            for key in ("provider", "api_base", "provider_model", "operation", "delivery")
        )
    )

"""Verified Bailian Beijing pay-as-you-go reference prices, never relay or account tariffs.

Only exact public model IDs are listed. Endpoint aliases and unlisted models require
an administrator override; absence is deliberately not a zero price.
"""

from copy import deepcopy
from decimal import Decimal

from keystone.platform.model_access.channels.rules import tier_rule, token_rule
from keystone.platform.model_access.schemas.pricing import OperationPricing

BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"
SOURCE = {
    "label": "百炼北京地域按量原价；不抵扣免费额度、资源包或优惠",
    "url": "https://help.aliyun.com/zh/model-studio/model-pricing",
    "version": "2026-10-30.2",
    "checked_at": "2026-10-30T00:00:00Z",
}
CACHE_SOURCE = {
    **SOURCE,
    "version": "2026-11-05.1",
    "label": "百炼北京实时推理原价及隐式缓存命中价；不含显式缓存、免费额度或限时折扣",
}


def ali_tiers(rows):
    # Official implicit-cache tariff: 20% for these explicitly enumerated families.
    # Newer Qwen 3.8 exceptions require a console quote; never infer their cache price.
    return tier_rule(
        [
            (limit, incoming, outgoing, format(Decimal(incoming) * Decimal("0.2"), "f"))
            for limit, incoming, outgoing in rows
        ]
    )


CHAT_RULES = {
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


_prefix_leaves(CHAT_RULES["qwen-plus"]["root"]["else"], "thinking_")
# A thinking request that reports zero reasoning uses the non-thinking output tariff.
# Missing reasoning usage remains unknown, rather than assuming the request took effect.
_qwen_price = CHAT_RULES["qwen-plus"]["root"]
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
    CHAT_RULES[alias] = CHAT_RULES[target]


def applies(context):
    # Explicit-cache requests have a different tariff that is not listed.
    return context.get("cache_mode") != "explicit"


REFERENCE_PRICES = {
    **{
        ("aliyun_bailian", BASE, "qwen-max", operation, "sync"): OperationPricing(
            operation=operation, rule=token_rule("2.4", "9.6"), source=SOURCE
        )
        for operation in ("text.generate", "text.structured")
    },
    **{
        ("aliyun_bailian", BASE, model, "text.embed", "sync"): OperationPricing(
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
    **{
        ("aliyun_bailian", BASE, model, operation, "sync"): OperationPricing(
            operation=operation, rule=rule, source=CACHE_SOURCE
        )
        for model, rule in CHAT_RULES.items()
        for operation in ("text.generate", "text.structured")
    },
}

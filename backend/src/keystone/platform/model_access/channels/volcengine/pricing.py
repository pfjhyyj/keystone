"""Verified Ark Beijing online standard-service reference prices."""

from keystone.platform.model_access.channels.rules import tier_rule
from keystone.platform.model_access.channels.shared.metering import CHAT_PROFILE
from keystone.platform.model_access.schemas.pricing import OperationPricing

PROFILE = CHAT_PROFILE.model_copy(deep=True)
PROFILE.id = "volcengine_chat.v2"
PROFILE.context_fields["context.service_tier"] = "string"
PROFILE.context_enums["context.service_tier"] = ["default", "auto", "flex", "fast"]
CONTEXT_DEFAULTS = {PROFILE.id: {"service_tier": "default"}}

BASE = "https://ark.cn-beijing.volces.com/api/v3"
SOURCE = {
    "label": "方舟北京地域在线标准服务原价；不含缓存存储、资源包或优惠",
    "url": "https://docs.volcengine.com/docs/ark/model-pricing?lang=zh",
    "version": "2026-10-30.2",
    "checked_at": "2026-10-30T00:00:00Z",
}

# API model IDs, not console display names or user-owned ep-* aliases. Ark's
# charge-item bounds use 32K=32768 (GetModelActivation.MultiChargeItems), unlike Ali.
CHAT_RULES = {
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


def applies(context):
    # Only the default service tier is priced; omitted requests default to it.
    return context.get("service_tier") == "default"


REFERENCE_PRICES = {
    ("volcengine", BASE, model, operation, "sync"): OperationPricing(
        operation=operation, rule=rule, source=SOURCE
    )
    for model, rule in CHAT_RULES.items()
    for operation in ("text.generate", "text.structured")
}

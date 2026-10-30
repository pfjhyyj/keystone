"""Verified reference prices, never relay prices or a live account tariff lookup.

Only exact public model IDs are listed. Endpoint aliases and unlisted models require
an administrator override; absence is deliberately not a zero price.
"""

from keystone.platform.model_access.schemas.pricing import OperationPricing

ALI_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"
SOURCE = {
    "label": "百炼北京地域按量原价；不抵扣免费额度、资源包或优惠",
    "url": "https://help.aliyun.com/zh/model-studio/model-pricing",
    "version": "2026-10-30.1",
    "checked_at": "2026-10-30T00:00:00Z",
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


def reference(binding):
    return REFERENCE_RULES.get(
        tuple(
            binding[key]
            for key in ("provider", "api_base", "provider_model", "operation", "delivery")
        )
    )

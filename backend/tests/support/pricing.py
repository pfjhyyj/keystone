"""Illustrative, explicitly configured test tariffs. Never official supplier prices."""

SOURCE = {"label": "Test procurement tariff", "version": "test.1"}


def token_rule(
    *, cached=False, writes=False, input_rate="2", output_rate="8", currency="CNY", factor="1"
):
    return {
        "format": 1,
        "currency": currency,
        "root": {
            "kind": "price",
            "id": "standard",
            "input_partition": "cache" if cached else "aggregate",
            "output_partition": "aggregate",
            "factor": factor,
            "items": [
                {
                    "id": "input",
                    "quantity": "usage.input_regular_tokens"
                    if cached
                    else "usage.input_total_tokens",
                    "rate": input_rate,
                    "per": 1000000,
                },
                *(
                    [
                        {
                            "id": "cache",
                            "quantity": "usage.input_cache_read_tokens",
                            "rate": "0.2",
                            "per": 1000000,
                        }
                    ]
                    if cached
                    else []
                ),
                *(
                    [
                        {
                            "id": "write",
                            "quantity": "usage.input_cache_write_tokens",
                            "rate": "2.5",
                            "per": 1000000,
                        }
                    ]
                    if writes
                    else []
                ),
                {
                    "id": "output",
                    "quantity": "usage.output_total_tokens",
                    "rate": output_rate,
                    "per": 1000000,
                },
            ],
        },
    }


def image_rule(rate="0.2", *, request=False):
    return {
        "format": 1,
        "currency": "CNY",
        "root": {
            "kind": "price",
            "id": "image",
            "input_partition": "none",
            "output_partition": "none",
            "factor": "1",
            "items": [
                {
                    "id": "image",
                    "quantity": "events.upstream_success_count"
                    if request
                    else "usage.generated_images",
                    "rate": rate,
                    "per": 1,
                }
            ],
        },
    }


def ttl_rule(*, input_rate="2", output_rate="8", five="2.5", one="4", currency="CNY"):
    """Cache writes split by reported lifetime; both components are disjoint subsets."""
    return {
        "format": 1,
        "currency": currency,
        "root": {
            "kind": "price",
            "id": "standard",
            "input_partition": "cache_ttl",
            "output_partition": "aggregate",
            "factor": "1",
            "items": [
                {
                    "id": "input",
                    "quantity": "usage.input_regular_tokens",
                    "rate": input_rate,
                    "per": 1000000,
                },
                {
                    "id": "cache_read",
                    "quantity": "usage.input_cache_read_tokens",
                    "rate": "0.2",
                    "per": 1000000,
                },
                {
                    "id": "write_5m",
                    "quantity": "usage.input_cache_write_5m_tokens",
                    "rate": five,
                    "per": 1000000,
                },
                {
                    "id": "write_1h",
                    "quantity": "usage.input_cache_write_1h_tokens",
                    "rate": one,
                    "per": 1000000,
                },
                {
                    "id": "output",
                    "quantity": "usage.output_total_tokens",
                    "rate": output_rate,
                    "per": 1000000,
                },
            ],
        },
    }


def config(rule=None, operation="text.generate", delivery="sync"):
    return {
        "mode": "override",
        "rules": [
            {
                "operation": operation,
                "delivery": delivery,
                "rule": rule or token_rule(),
                "source": SOURCE,
            }
        ],
    }

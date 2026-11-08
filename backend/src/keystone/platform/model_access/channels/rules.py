"""Reusable reference-price rule builders; each channel owns its own tariff data."""


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

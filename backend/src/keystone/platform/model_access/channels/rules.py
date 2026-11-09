"""Reusable reference-price rule builders; each channel owns its own tariff data."""

from decimal import Decimal


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


def _tiers(rows, leaves):
    root = leaves[-1]
    for (threshold, *_), leaf in reversed(list(zip(rows[:-1], leaves[:-1], strict=True))):
        root = {
            "kind": "branch",
            "when": {"field": "usage.input_total_tokens", "op": "lte", "value": threshold},
            "then": leaf,
            "else": root,
        }
    return {"format": 1, "currency": "CNY", "root": root}


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
    return _tiers(rows, leaves)


def share(rate, factor):
    """A derived price such as 125% of this tier's ordinary input price."""
    return format(Decimal(rate) * Decimal(factor), "f")


def cache_tier_rule(rows, *, read, write=None, split=None):
    """Whole-request tiers whose input partition separates cache reads and writes.

    ``read``/``write`` are absolute prices or ``input_rate`` callables; ``split`` is a
    ``(5m, 1h)`` pair replacing ``write`` for upstreams that report write lifetimes.
    Every possible nonzero component of the chosen partition is priced explicitly.
    """
    leaves = []
    for index, (_, incoming, outgoing) in enumerate(rows):
        leaf = token_rule(incoming, outgoing)["root"]
        leaf["id"] = f"tier_{index}"
        if split:
            leaf["input_partition"] = "cache_ttl"
            leaf["items"][0]["quantity"] = "usage.input_regular_tokens"
            components = (
                ("cache_read", "usage.input_cache_read_tokens", read),
                ("cache_write_5m", "usage.input_cache_write_5m_tokens", split[0]),
                ("cache_write_1h", "usage.input_cache_write_1h_tokens", split[1]),
            )
        else:
            leaf["input_partition"] = "cache" if write is not None else "cache_read"
            leaf["items"][0]["quantity"] = (
                "usage.input_regular_tokens" if write is not None else "usage.input_uncached_tokens"
            )
            components = (
                ("cache_read", "usage.input_cache_read_tokens", read),
                ("cache_write", "usage.input_cache_write_tokens", write),
            )
        leaf["items"].extend(
            {
                "id": name,
                "quantity": quantity,
                "rate": rate(incoming) if callable(rate) else rate,
                "per": 1000000,
            }
            for name, quantity, rate in components
            if rate is not None
        )
        leaves.append(leaf)
    return _tiers(rows, leaves)


def by_cache_mode(rule, explicit_root):
    """Add an explicit-cache branch to an implicit-only rule document."""
    return {
        **rule,
        "root": {
            "kind": "branch",
            "when": {"field": "context.cache_mode", "op": "eq", "value": "explicit"},
            "then": explicit_root,
            "else": rule["root"],
        },
    }

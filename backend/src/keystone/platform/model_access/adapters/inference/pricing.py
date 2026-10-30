"""Protocol-owned metering. Never infer tokens from bodies, sizes or requested n."""

import json
from datetime import datetime
from email.parser import BytesParser
from email.policy import default
from zoneinfo import ZoneInfo

from keystone.platform.model_access.schemas.pricing import (
    Measurement,
    MetricProfile,
    PricingFacts,
)

INPUT = {
    "none": [],
    "aggregate": ["usage.input_total_tokens"],
    "cache": ["usage.input_regular_tokens", "usage.input_cache_read_tokens"],
}
CACHE_WRITES = {**INPUT, "cache": [*INPUT["cache"], "usage.input_cache_write_tokens"]}
OUTPUT = {
    "none": [],
    "aggregate": ["usage.output_total_tokens"],
    "reasoning": ["usage.output_regular_tokens", "usage.reasoning_tokens"],
}
MODAL_INPUT = ["usage.input_text_tokens", "usage.input_image_tokens"]
MODAL_OUTPUT = ["usage.output_text_tokens", "usage.output_image_tokens"]


def _profile(name, inputs, outputs, context, *, images=False, zeros=()):
    tokens = {key for parts in (inputs, outputs) for fields in parts.values() for key in fields}
    return MetricProfile(
        id=f"{name}.v1",
        quantities={
            **{key: "token" for key in tokens},
            "events.upstream_success_count": "request",
            **({"usage.generated_images": "image"} if images else {}),
        },
        input_partitions=inputs,
        output_partitions=outputs,
        context_fields=context,
        structural_zero=list(zeros),
    )


PROFILES = {
    "chat.v1": _profile("chat", INPUT, OUTPUT, {"context.enable_thinking": "integer"}),
    "relay_chat.v1": _profile(
        "relay_chat",
        CACHE_WRITES,
        OUTPUT,
        {"context.enable_thinking": "integer", "context.cache_ttl": "string"},
    ),
    "embedding.v1": _profile(
        "embedding",
        {"none": [], "aggregate": INPUT["aggregate"]},
        {"none": [], "aggregate": OUTPUT["aggregate"]},
        {"context.dimensions": "integer"},
        zeros=("usage.output_total_tokens",),
    ),
    "openrouter_images.v1": _profile(
        "openrouter_images",
        CACHE_WRITES,
        OUTPUT,
        {
            "context.resolution": "string",
            "context.output_format": "string",
            "context.reference_images": "integer",
        },
        images=True,
    ),
    "openai_images.v1": _profile(
        "openai_images",
        {**INPUT, "modality": MODAL_INPUT},
        {"none": [], "aggregate": OUTPUT["aggregate"], "modality": MODAL_OUTPUT},
        {
            "context.size": "string",
            "context.quality": "string",
            "context.output_format": "string",
            "context.reference_images": "integer",
        },
        images=True,
    ),
    "kling_images.v1": _profile(
        "kling_images", {"none": []}, {"none": []}, {"context.resolution": "string"}, images=True
    ),
}
for name, enums in {
    "chat.v1": {"context.enable_thinking": [0, 1]},
    "relay_chat.v1": {"context.enable_thinking": [0, 1], "context.cache_ttl": ["5m", "1h"]},
    "openrouter_images.v1": {
        "context.resolution": ["1K", "2K", "4K"],
        "context.output_format": ["png", "jpeg", "webp"],
    },
    "openai_images.v1": {
        "context.quality": ["low", "medium", "high"],
        "context.output_format": ["png", "jpeg", "webp"],
    },
    "kling_images.v1": {"context.resolution": ["1k", "2k"]},
}.items():
    PROFILES[name].context_enums = enums


def merge_usage(previous, incoming):
    """Cumulative receipts replace fields, never add chunk totals or retain arbitrary JSON."""
    result = dict(previous or {})
    for key in (
        "prompt_tokens",
        "completion_tokens",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "prompt_cache_hit_tokens",
        "prompt_cache_miss_tokens",
    ):
        if key in incoming:
            value = incoming[key]
            result[key] = value if type(value) is int else -1
    for key in (
        "prompt_tokens_details",
        "completion_tokens_details",
        "input_tokens_details",
        "output_tokens_details",
    ):
        if key not in incoming:
            continue
        details = incoming[key]
        if not isinstance(details, dict):
            result[key] = {"cached_tokens": -1}
            continue
        target = dict(result.get(key) or {})
        for field in (
            "cached_tokens",
            "cache_write_tokens",
            "reasoning_tokens",
            "text_tokens",
            "image_tokens",
        ):
            if field in details:
                value = details[field]
                target[field] = value if type(value) is int else -1
        result[key] = target
    return result


def profile_for(protocol):
    # Explicit installation; a new protocol must opt into its own metering contract.
    from .embeddings import Embeddings
    from .kling import KlingImages
    from .openai_images import OpenAIImages
    from .protocols import ChatCompletions, OpenRouterImages

    for protocol_type, name in (
        (KlingImages, "kling_images.v1"),
        (OpenAIImages, "openai_images.v1"),
        (OpenRouterImages, "openrouter_images.v1"),
        (Embeddings, "embedding.v1"),
        (ChatCompletions, "chat.v1"),
    ):
        if isinstance(protocol, protocol_type):
            return PROFILES[
                protocol.pricing_meter if isinstance(protocol, ChatCompletions) else name
            ]
    raise ValueError("No installed pricing meter")


def request_context(request, profile):
    """Called only after the existing protocol validator accepted the exact bytes."""
    if "multipart/form-data" in request.headers.get("content-type", ""):
        message = BytesParser(policy=default).parsebytes(
            b"Content-Type: "
            + request.headers["content-type"].encode()
            + b"\r\n\r\n"
            + request.content
        )
        body, references = {}, 0
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if name == "image":
                references += 1
            elif f"context.{name}" in profile.context_fields:
                body[name] = part.get_payload(decode=True).decode()
        body["reference_images"] = references
    else:
        body = json.loads(request.content)
        if profile.id in {"openrouter_images.v1", "openai_images.v1"}:
            body["reference_images"] = len(body.get("input_references", []))
    result = {}
    if "context.cache_ttl" in profile.context_fields:
        ttls = set()
        unknown_ttl = False
        for message in body.get("messages", []):
            parts = message.get("content")
            for part in parts if isinstance(parts, list) else []:
                control = part.get("cache_control")
                if isinstance(control, dict) and control.get("type") == "ephemeral":
                    ttl = control.get("ttl", "5m")
                    if isinstance(ttl, str) and ttl in {"5m", "1h"}:
                        ttls.add(ttl)
                    else:
                        unknown_ttl = True
        if not unknown_ttl and len(ttls) == 1:
            result["cache_ttl"] = next(iter(ttls))
    for field, kind in profile.context_fields.items():
        key = field.removeprefix("context.")
        value = body.get(key)
        if key == "enable_thinking" and type(value) is bool:
            value = int(value)
        if type(value) is (int if kind == "integer" else str):
            result[key] = value
    return result


def normalize_pricing_facts(profile, usage, *, complete=False, success=False, generated=None):
    result = PricingFacts(profile=profile.id)

    def put(key, value, source="upstream_usage", *, final=None):
        key = f"usage.{key}" if "." not in key else key
        if key not in profile.quantities or value is None:
            return
        if type(value) is not int or not 0 <= value < 2**63:
            result.error = "meter_conflict"
            return
        result.values[key] = Measurement(
            value=value,
            unit=profile.quantities[key],
            source=source,
            complete=complete if final is None else final,
        )

    data = usage if isinstance(usage, dict) else {}
    # Only the currently installed OpenAI-compatible wire contracts are normalized.
    image_wire = profile.id == "openai_images.v1"
    put("input_total_tokens", data.get("input_tokens" if image_wire else "prompt_tokens"))
    put("output_total_tokens", data.get("output_tokens" if image_wire else "completion_tokens"))
    details = data.get("input_tokens_details" if image_wire else "prompt_tokens_details")
    details = details if isinstance(details, dict) else {}
    cached = details.get("cached_tokens", data.get("prompt_cache_hit_tokens"))
    put("input_cache_read_tokens", cached)
    write = details.get("cache_write_tokens")
    put("input_cache_write_tokens", write)
    if write not in (None, 0) and "usage.input_cache_write_tokens" not in profile.quantities:
        result.error = "meter_conflict"
    out = data.get("output_tokens_details" if image_wire else "completion_tokens_details")
    out = out if isinstance(out, dict) else {}
    put("reasoning_tokens", out.get("reasoning_tokens"))
    if profile.id == "embedding.v1":
        if "usage.input_total_tokens" not in result.values:
            put("input_total_tokens", data.get("total_tokens"), "derived")
        put("output_total_tokens", 0, "protocol")
    for side, detail in (("input", details), ("output", out)):
        for modality in ("text", "image"):
            put(f"{side}_{modality}_tokens", detail.get(f"{modality}_tokens"))
    for total, subset, remainder in (
        ("input_total_tokens", "input_cache_read_tokens", "input_regular_tokens"),
        ("output_total_tokens", "reasoning_tokens", "output_regular_tokens"),
    ):
        a, b = (result.values.get(f"usage.{key}") for key in (total, subset))
        if a is not None and b is not None:
            written = (
                result.values.get("usage.input_cache_write_tokens")
                if total == "input_total_tokens"
                else None
            )
            if (
                total == "input_total_tokens"
                and "usage.input_cache_write_tokens" in profile.quantities
                and written is None
            ):
                continue
            deducted = b.value + (written.value if written else 0)
            if deducted > a.value:
                result.error = "meter_conflict"
            else:
                put(remainder, a.value - deducted, "derived")
    raw_total = data.get("total_tokens")
    if raw_total is not None:
        a, b = (result.values.get(f"usage.{key}_total_tokens") for key in ("input", "output"))
        if (
            type(raw_total) is not int
            or not 0 <= raw_total < 2**63
            or a is not None
            and b is not None
            and a.value + b.value != raw_total
        ):
            result.error = "meter_conflict"
    miss = data.get("prompt_cache_miss_tokens")
    regular = result.values.get("usage.input_regular_tokens")
    if miss is not None and (
        type(miss) is not int
        or not 0 <= miss < 2**63
        or regular is not None
        and miss != regular.value
    ):
        result.error = "meter_conflict"
    if generated is not None:
        put("generated_images", generated, "upstream_result", final=True)
    if success:
        put("events.upstream_success_count", 1, "upstream_result", final=True)
    return result


def observed_time_context(rule, observed_at: datetime | None):
    basis = rule.time_basis if rule else None
    if basis is None or basis.source != "observed_response" or observed_at is None:
        return {}
    local = observed_at.astimezone(ZoneInfo(basis.timezone))
    return {
        "pricing_hour": local.hour,
        "pricing_weekday": local.weekday(),
        "pricing_date": local.date().isoformat(),
    }

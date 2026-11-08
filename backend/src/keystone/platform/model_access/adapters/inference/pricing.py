"""Protocol-owned metering. Never infer tokens from bodies, sizes or requested n."""

import json
from datetime import datetime
from email.parser import BytesParser
from email.policy import default
from zoneinfo import ZoneInfo

from keystone.platform.model_access.channels import INSTALLED
from keystone.platform.model_access.schemas.pricing import Measurement, PricingFacts

# Channel implementations depend only on pure declarations, never on this engine.
PROFILES = {}
CONTEXT_DEFAULTS = {}
for spec in INSTALLED:
    for profile in spec.pricing_profiles:
        if profile.id in PROFILES and PROFILES[profile.id] != profile:
            raise ValueError(f"Conflicting pricing profile: {profile.id}")
        PROFILES[profile.id] = profile
    for meter, defaults in spec.pricing_context_defaults.items():
        if meter not in {profile.id for profile in spec.pricing_profiles}:
            raise ValueError(f"Undeclared pricing profile defaults: {meter}")
        if meter in CONTEXT_DEFAULTS and CONTEXT_DEFAULTS[meter] != defaults:
            raise ValueError(f"Conflicting pricing context defaults: {meter}")
        CONTEXT_DEFAULTS[meter] = defaults


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
        "reasoning_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    ):
        if key in incoming:
            value = incoming[key]
            result[key] = value if type(value) is int or value is None else -1
    for key in (
        "prompt_tokens_details",
        "completion_tokens_details",
        "input_tokens_details",
        "output_tokens_details",
    ):
        if key not in incoming:
            continue
        details = incoming[key]
        if details is None:
            result.pop(key, None)
            continue
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
            "cache_creation_input_tokens",
            "audio_tokens",
        ):
            if field in details:
                value = details[field]
                target[field] = value if type(value) is int or value is None else -1
        result[key] = target
    return result


def profile_for(protocol):
    # Explicit installation; a new protocol must opt into its own metering contract.
    meter = getattr(protocol, "pricing_meter", None)
    if meter not in PROFILES:
        raise ValueError("No installed pricing meter")
    return PROFILES[meter]


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
        if "context.reference_images" in profile.context_fields:
            body["reference_images"] = len(body.get("input_references", []))
    result = {}
    if "context.cache_mode" in profile.context_fields:
        controls = [*body.get("messages", []), *body.get("tools", [])]
        for message in body.get("messages", []):
            if isinstance(message.get("content"), list):
                controls.extend(message["content"])
        result["cache_mode"] = (
            "explicit"
            if any(isinstance(part, dict) and "cache_control" in part for part in controls)
            else "implicit"
        )
    for key, value in CONTEXT_DEFAULTS.get(profile.id, {}).items():
        if key not in body:
            result[key] = value
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
        if key in {"cache_mode", "cache_ttl"}:
            continue
        value = body.get(key)
        if key == "enable_thinking" and type(value) is bool:
            value = int(value)
        if type(value) is (int if kind == "integer" else str):
            result[key] = value
    return result


def _measure(profile, usage, *, complete, success, generated):
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
    image_wire = profile.id == "openai_images.v2"
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
    if profile.id == "embedding.v2":
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


def normalize_pricing_facts(profile, usage, *, complete=False, success=False, generated=None):
    """OpenAI-compatible aliases agree; absence is distinct from a reported zero."""
    data = usage if isinstance(usage, dict) else {}
    invalid = False

    def number(*values):
        nonlocal invalid
        present = [value for value in values if value is not None]
        if any(type(value) is not int or not 0 <= value < 2**63 for value in present):
            invalid = True
            return None
        if len(set(present)) > 1:
            invalid = True
        return present[0] if present else None

    def details(*names):
        nonlocal invalid
        objects = []
        for name in names:
            value = data.get(name)
            if value is None:
                continue
            if not isinstance(value, dict):
                invalid = True
                continue
            objects.append(value)
        fields = (
            "cached_tokens",
            "cache_write_tokens",
            "cache_creation_input_tokens",
            "reasoning_tokens",
            "text_tokens",
            "image_tokens",
            "audio_tokens",
        )
        result = {key: number(*(value.get(key) for value in objects)) for key in fields}
        # Audio has no installed pricing meter. Never price it as ordinary text.
        if result.pop("audio_tokens") not in (None, 0):
            invalid = True
        result["cache_write_tokens"] = number(
            result["cache_write_tokens"], result.pop("cache_creation_input_tokens")
        )
        return {key: value for key, value in result.items() if value is not None}

    inputs = number(data.get("prompt_tokens"), data.get("input_tokens"))
    outputs = number(data.get("completion_tokens"), data.get("output_tokens"))
    prompt = details("prompt_tokens_details", "input_tokens_details")
    output = details("completion_tokens_details", "output_tokens_details")
    hit = number(prompt.get("cached_tokens"), data.get("prompt_cache_hit_tokens"))
    miss = number(data.get("prompt_cache_miss_tokens"))
    reasoning = number(output.get("reasoning_tokens"), data.get("reasoning_tokens"))
    derived = set()
    if inputs is None and hit is not None and miss is not None:
        inputs = number(hit + miss)
        derived.add("input_total_tokens")
    if hit is None and inputs is not None and miss is not None:
        hit = number(inputs - miss)
        derived.add("input_cache_read_tokens")
    if inputs is not None and hit is not None and miss is not None and inputs != hit + miss:
        invalid = True
    # Native Anthropic input excludes its flat cache counters; that wire is not installed.
    if any(
        number(data.get(key)) not in (None, 0)
        for key in ("cache_read_input_tokens", "cache_creation_input_tokens")
    ):
        invalid = True
    if hit is not None:
        prompt["cached_tokens"] = hit
    if reasoning is not None:
        output["reasoning_tokens"] = reasoning
    if profile.id == "embedding.v2" and outputs not in (None, 0):
        invalid = True
    image = profile.id == "openai_images.v2"
    canonical = {
        "input_tokens" if image else "prompt_tokens": inputs,
        "output_tokens" if image else "completion_tokens": outputs,
        "input_tokens_details" if image else "prompt_tokens_details": prompt,
        "output_tokens_details" if image else "completion_tokens_details": output,
        "total_tokens": number(data.get("total_tokens")),
    }
    result = _measure(
        profile,
        canonical,
        complete=complete,
        success=success,
        generated=generated,
    )
    if (
        "usage.input_uncached_tokens" in profile.quantities
        and inputs is not None
        and hit is not None
    ):
        uncached = number(inputs - hit)
        if uncached is not None:
            result.values["usage.input_uncached_tokens"] = Measurement(
                value=uncached,
                unit="token",
                source="upstream_usage" if miss is not None else "derived",
                complete=complete,
            )
    for key in derived:
        if fact := result.values.get(f"usage.{key}"):
            fact.source = "derived"
    if invalid:
        result.error = "meter_conflict"
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

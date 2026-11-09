"""Pure metering declarations; no installed-channel registry or inference engine imports."""

from keystone.platform.model_access.schemas.pricing import MetricProfile

INPUT = {
    "none": [],
    "aggregate": ["usage.input_total_tokens"],
    "cache": ["usage.input_regular_tokens", "usage.input_cache_read_tokens"],
}
CACHE_WRITES = {**INPUT, "cache": [*INPUT["cache"], "usage.input_cache_write_tokens"]}
# v3 adds cache writes with their reported lifetime split. Splits are disjoint subsets of the
# write total, so a rule picks exactly one write partition and never prices both.
CACHE_WRITES_V3 = {
    **CACHE_WRITES,
    "cache_ttl": [
        "usage.input_regular_tokens",
        "usage.input_cache_read_tokens",
        "usage.input_cache_write_5m_tokens",
        "usage.input_cache_write_1h_tokens",
    ],
}
OUTPUT = {
    "none": [],
    "aggregate": ["usage.output_total_tokens"],
    "reasoning": ["usage.output_regular_tokens", "usage.reasoning_tokens"],
}
MODAL_INPUT = ["usage.input_text_tokens", "usage.input_image_tokens"]
MODAL_OUTPUT = ["usage.output_text_tokens", "usage.output_image_tokens"]


def make_profile(name, inputs, outputs, context, *, images=False, zeros=(), enums=None):
    tokens = {key for parts in (inputs, outputs) for fields in parts.values() for key in fields}
    profile = MetricProfile(
        id=name,
        quantities={
            **{key: "token" for key in tokens},
            "events.upstream_success_count": "request",
            **({"usage.generated_images": "image"} if images else {}),
        },
        input_partitions=inputs,
        output_partitions=outputs,
        context_fields=context,
        context_enums=enums or {},
        structural_zero=list(zeros),
    )
    if "cache" in profile.input_partitions:
        profile.quantities["usage.input_uncached_tokens"] = "token"
        profile.input_partitions["cache_read"] = [
            "usage.input_uncached_tokens",
            "usage.input_cache_read_tokens",
        ]
        profile.context_fields["context.cache_mode"] = "string"
        profile.context_enums["context.cache_mode"] = ["implicit", "explicit"]
    return profile


CHAT_PROFILE = make_profile(
    "chat.v2",
    INPUT,
    OUTPUT,
    {"context.enable_thinking": "integer"},
    enums={"context.enable_thinking": [0, 1]},
)
RELAY_CHAT_PROFILE = make_profile(
    "relay_chat.v2",
    CACHE_WRITES,
    OUTPUT,
    {"context.enable_thinking": "integer", "context.cache_ttl": "string"},
    enums={"context.enable_thinking": [0, 1], "context.cache_ttl": ["5m", "1h"]},
)
# Installed meters. v2 stays registered for snapshots frozen before the write partition existed.
# cache_ttl describes the request, not the writes a response reports; rules price writes from usage.
CHAT_PROFILE_V3 = make_profile(
    "chat.v3",
    CACHE_WRITES_V3,
    OUTPUT,
    {"context.enable_thinking": "integer", "context.cache_ttl": "string"},
    enums={"context.enable_thinking": [0, 1], "context.cache_ttl": ["5m", "1h"]},
)
RELAY_CHAT_PROFILE_V3 = make_profile(
    "relay_chat.v3",
    CACHE_WRITES_V3,
    OUTPUT,
    {"context.enable_thinking": "integer", "context.cache_ttl": "string"},
    enums={"context.enable_thinking": [0, 1], "context.cache_ttl": ["5m", "1h"]},
)
EMBEDDING_PROFILE = make_profile(
    "embedding.v2",
    {"none": [], "aggregate": INPUT["aggregate"]},
    {"none": [], "aggregate": OUTPUT["aggregate"]},
    {"context.dimensions": "integer"},
    zeros=("usage.output_total_tokens",),
)
OPENAI_IMAGES_PROFILE = make_profile(
    "openai_images.v2",
    {**INPUT, "modality": MODAL_INPUT},
    {"none": [], "aggregate": OUTPUT["aggregate"], "modality": MODAL_OUTPUT},
    {
        "context.size": "string",
        "context.quality": "string",
        "context.output_format": "string",
        "context.reference_images": "integer",
    },
    images=True,
    enums={
        "context.quality": ["low", "medium", "high"],
        "context.output_format": ["png", "jpeg", "webp"],
    },
)

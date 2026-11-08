"""OpenRouter image generation through its unified /images endpoint, without fallbacks."""

from keystone.platform.model_access.channels.shared import images
from keystone.platform.model_access.channels.shared.metering import (
    CACHE_WRITES,
    OUTPUT,
    make_profile,
)
from keystone.platform.model_access.schemas.errors import AdapterError

PROFILE = make_profile(
    "openrouter_images.v2",
    CACHE_WRITES,
    OUTPUT,
    {
        "context.resolution": "string",
        "context.output_format": "string",
        "context.reference_images": "integer",
    },
    images=True,
    enums={
        "context.resolution": ["1K", "2K", "4K"],
        "context.output_format": ["png", "jpeg", "webp"],
    },
)


class OpenRouterImages:
    pricing_meter = "openrouter_images.v2"
    operations = frozenset({"image.generate", "image.edit"})
    delivery = "sync"
    models = None
    paths = {"/images": "POST"}
    require_parameters = False

    def request_target(self, selected):
        return selected.api_base, "/images", "model"

    async def invoke(self, sdk, selected, data, inputs, identifier, digest, run_sync):
        if data.aspect_ratio is not None or data.quality is not None:
            raise AdapterError(422, "model_invalid_request", "该图片协议不支持比例或画质参数")
        references = []
        for content_type, content, _ in inputs:
            encoded = await run_sync(images.image_data_uri, content, content_type)
            references.append({"type": "image_url", "image_url": {"url": encoded}})
        body = data.model_dump(mode="json", exclude={"images", "aspect_ratio", "quality"})
        body["model"] = selected.provider_model
        body["provider"] = {"allow_fallbacks": False}
        if selected.operation == "image.edit":
            body["input_references"] = references
        response = await sdk.post("/images", body=body, cast_to=dict)
        return await run_sync(
            images.image_batch, response, data, identifier, digest, selected.operation
        )

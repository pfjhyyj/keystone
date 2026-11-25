"""Verified OpenAI Standard-tier GPT Image reference prices, in USD."""

from keystone.platform.model_access.schemas.pricing import OperationPricing

BASE = "https://api.openai.com/v1"
SOURCE = {
    "label": "OpenAI Standard 价格；/images 不适用缓存输入价",
    "url": "https://developers.openai.com/api/docs/pricing",
    "version": "2026-11-25",
    "checked_at": "2026-11-25T00:00:00Z",
}


def image_rule(text_input, image_input, image_output):
    """Per 1M tokens by modality; text output is not charged but stays explicit."""

    def item(name, quantity, rate):
        return {"id": name, "quantity": quantity, "rate": rate, "per": 1000000}

    return {
        "format": 1,
        "currency": "USD",
        "root": {
            "kind": "price",
            "id": "standard",
            "input_partition": "modality",
            "output_partition": "modality",
            "factor": "1",
            "items": [
                item("input_text", "usage.input_text_tokens", text_input),
                item("input_image", "usage.input_image_tokens", image_input),
                item("output_text", "usage.output_text_tokens", "0"),
                item("output_image", "usage.output_image_tokens", image_output),
            ],
        },
    }


# Cached input rates apply only to Responses API image generation, not to /images.
IMAGE_RULES = dict.fromkeys(
    ("gpt-image-2", "gpt-image-2.5-sunburst", "gpt-image-2.5-flare"),
    image_rule("5.00", "8.00", "30.00"),
)

REFERENCE_PRICES = {
    ("openai", BASE, model, operation, "sync"): OperationPricing(
        operation=operation, rule=rule, source=SOURCE
    )
    for model, rule in IMAGE_RULES.items()
    for operation in ("image.generate", "image.edit")
}

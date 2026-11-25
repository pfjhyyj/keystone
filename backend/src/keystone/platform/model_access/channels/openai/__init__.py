"""OpenAI official API: fixed base, chat family, embeddings and GPT Image sync images."""

from dataclasses import replace

from keystone.platform.model_access.channels.openai import pricing
from keystone.platform.model_access.channels.openai.billing import OpenAI
from keystone.platform.model_access.channels.shared import CHAT, EMBEDDINGS, RELAY_IMAGES
from keystone.platform.model_access.channels.shared.metering import (
    CHAT_PROFILE_V3,
    EMBEDDING_PROFILE,
    OPENAI_IMAGES_PROFILE,
)
from keystone.platform.model_access.channels.spec import ChannelSpec

# Official models documented with arbitrary WIDTHxHEIGHT sizes; fixed-size models stay out.
IMAGE_MODELS = frozenset(
    {
        "gpt-image-2",
        "gpt-image-2-2026-04-21",
        "gpt-image-2.5-sunburst",
        "gpt-image-2.5-sunburst-2026-09-08",
        "gpt-image-2.5-flare",
        "gpt-image-2.5-flare-2026-09-08",
    }
)

SPEC = ChannelSpec(
    id="openai",
    label="OpenAI",
    api_base=OpenAI.api_base,
    protocols=(
        replace(CHAT, token_limit="max_completion_tokens"),
        EMBEDDINGS,
        # The official multipart edit form repeats image[], not the relays' image field.
        replace(RELAY_IMAGES, models=IMAGE_MODELS, image_field="image[]"),
    ),
    pricing_profiles=(CHAT_PROFILE_V3, EMBEDDING_PROFILE, OPENAI_IMAGES_PROFILE),
    billing=OpenAI(),
    models_path="/models",
    reference_prices=pricing.REFERENCE_PRICES,
    hint=(
        "OpenAI 官方地址固定为 https://api.openai.com/v1，使用普通 API Key。"
        "兼容代理请选择 NewAPI 或 Yunwu 类渠道。官方接口不提供按请求查费或 API Key 余额，"
        "费用仅按模型价格规则预估；GPT Image 内置 Standard 参考价（USD）。"
    ),
)

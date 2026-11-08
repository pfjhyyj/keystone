"""OpenRouter: fixed official base, strict structured routing and per-request cost lookup."""

from dataclasses import replace

from keystone.platform.model_access.channels.openrouter.billing import OpenRouter
from keystone.platform.model_access.channels.openrouter.images import (
    PROFILE,
    OpenRouterImages,
)
from keystone.platform.model_access.channels.shared import EMBEDDINGS, RELAY_CHAT
from keystone.platform.model_access.channels.shared.metering import (
    EMBEDDING_PROFILE,
    RELAY_CHAT_PROFILE,
)
from keystone.platform.model_access.channels.spec import ChannelSpec

SPEC = ChannelSpec(
    id="openrouter",
    label="OpenRouter",
    api_base=OpenRouter.api_base,
    protocols=(replace(RELAY_CHAT, require_parameters=True), OpenRouterImages(), EMBEDDINGS),
    pricing_profiles=(RELAY_CHAT_PROFILE, PROFILE, EMBEDDING_PROFILE),
    billing=OpenRouter(),
    models_path="/models",
    hint=(
        "OpenRouter 地址固定为 https://openrouter.ai/api/v1。推理使用普通 API Key；"
        "可选的账户余额查询使用同一账户的 Management Key。"
    ),
)

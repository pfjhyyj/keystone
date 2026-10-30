"""Installed channel compositions. These are code, never database protocol configuration."""

from collections.abc import Callable
from dataclasses import dataclass, replace

from keystone.platform.model_access.adapters.billing.aliyun_bailian import AliyunBailian
from keystone.platform.model_access.adapters.billing.deepseek import DeepSeek
from keystone.platform.model_access.adapters.billing.newapi import Newapi
from keystone.platform.model_access.adapters.billing.openlux import Openlux
from keystone.platform.model_access.adapters.billing.openrouter import OpenRouter
from keystone.platform.model_access.adapters.billing.volcengine import Volcengine
from keystone.platform.model_access.adapters.billing.yunwu import Yunwu
from keystone.platform.model_access.adapters.inference.dashscope import DashScopeChat
from keystone.platform.model_access.adapters.inference.embeddings import Embeddings
from keystone.platform.model_access.adapters.inference.kling import KlingImages
from keystone.platform.model_access.adapters.inference.openai_images import OpenAIImages
from keystone.platform.model_access.adapters.inference.protocols import (
    ChatCompletions,
    OpenRouterImages,
)
from keystone.platform.model_access.schemas.channels import normalize_api_base

OPENROUTER_BASE = "https://openrouter.ai/api/v1"


def bearer_auth(request, secret):
    request.headers["Authorization"] = "Bearer " + secret


@dataclass(frozen=True)
class ChannelAdapter:
    api_base: str
    protocols: tuple
    billing: object
    authorize: Callable = bearer_auth
    models_path: str | None = None

    def protocol(self, operation, delivery=None, *, model=None):
        return next(
            (
                protocol
                for protocol in self.protocols
                if operation in protocol.operations
                and (delivery is None or delivery == protocol.delivery)
                and (model is None or protocol.models is None or model in protocol.models)
            ),
            None,
        )

    def supports(self, operation, delivery, *, model=None):
        return self.protocol(operation, delivery, model=model) is not None

    def allows_target(self, base, path, method):
        return base == self.api_base and any(
            protocol.paths.get(path) == method for protocol in self.protocols
        )


# Multiple channel types reuse the same compatible implementation, with explicit limits.
CHAT = ChatCompletions(frozenset({"text.generate", "text.structured", "image.understand"}))
EMBEDDINGS = Embeddings()
RELAY_IMAGES = OpenAIImages()
CHANNELS = {
    "openrouter": ChannelAdapter(
        OPENROUTER_BASE,
        (
            replace(CHAT, require_parameters=True),
            OpenRouterImages(),
            EMBEDDINGS,
        ),
        OpenRouter(),
        models_path="/models",
    ),
    "openlux": ChannelAdapter(
        "https://api.openlux.ai/v1",
        (CHAT, RELAY_IMAGES, KlingImages()),
        Openlux(),
        models_path="/models",
    ),
    "yunwu": ChannelAdapter(
        "https://yunwu.ai/v1", (CHAT, RELAY_IMAGES), Yunwu(), models_path="/models"
    ),
    "newapi": ChannelAdapter(
        "https://newapiai.com/v1", (CHAT, EMBEDDINGS), Newapi(), models_path="/models"
    ),
    # DeepSeek retains its billing declaration without an inference protocol.
    "deepseek": ChannelAdapter("https://api.deepseek.com", (), DeepSeek()),
    "aliyun_bailian": ChannelAdapter(
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        (DashScopeChat(CHAT.operations), EMBEDDINGS),
        AliyunBailian(),
    ),
    "volcengine": ChannelAdapter("https://ark.cn-beijing.volces.com/api/v3", (CHAT,), Volcengine()),
}

# Historical provider IDs remain valid in saved channels, billing evidence and Flow snapshots.
LEGACY_NEWAPI = ("openlux", "yunwu")
PROVIDERS = ("openrouter", "newapi", "yunwu", "aliyun_bailian", "volcengine")


def channel_adapter(provider, base=None, *, channels=None):
    installed = CHANNELS if channels is None else channels
    adapter = installed.get(provider)
    if provider not in {"newapi", "yunwu"} or base is None or adapter is None:
        return adapter
    try:
        if normalize_api_base(base) != base:
            return None
    except (ValueError, UnicodeError):
        return None
    for legacy in LEGACY_NEWAPI:
        site = installed.get(legacy)
        if site is not None and base == site.api_base:
            return site
    return replace(
        adapter,
        api_base=base,
        protocols=(CHAT,) if provider == "yunwu" else adapter.protocols,
        billing=adapter.billing.for_base(base)
        if isinstance(adapter.billing, (Newapi, Yunwu))
        else adapter.billing,
    )


def channel_targets():
    """Catalog predicates: compatible chat/vision plus site-specific image generation."""
    for provider, adapter in CHANNELS.items():
        yield (
            provider,
            None if provider in {"newapi", "yunwu"} else adapter.api_base,
            (replace(adapter, protocols=(CHAT,)) if provider == "yunwu" else adapter),
        )
    for provider in ("newapi", "yunwu"):
        for legacy in LEGACY_NEWAPI:
            adapter = CHANNELS[legacy]
            yield provider, adapter.api_base, adapter

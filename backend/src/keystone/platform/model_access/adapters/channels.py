"""Installed channel compositions. These are code, never database protocol configuration."""

from collections.abc import Callable
from dataclasses import dataclass

from keystone.platform.model_access.adapters.billing.aliyun_bailian import AliyunBailian
from keystone.platform.model_access.adapters.billing.deepseek import DeepSeek
from keystone.platform.model_access.adapters.billing.openlux import Openlux
from keystone.platform.model_access.adapters.billing.openrouter import OpenRouter
from keystone.platform.model_access.adapters.billing.yunwu import Yunwu
from keystone.platform.model_access.adapters.inference.kling import KlingImages
from keystone.platform.model_access.adapters.inference.protocols import (
    ChatCompletions,
    OpenRouterImages,
)

OPENROUTER_BASE = "https://openrouter.ai/api/v1"


def bearer_auth(request, secret):
    request.headers["Authorization"] = "Bearer " + secret


@dataclass(frozen=True)
class ChannelAdapter:
    api_base: str
    protocols: tuple
    billing: object
    authorize: Callable = bearer_auth

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
CHAT = ChatCompletions()
CHANNELS = {
    "openrouter": ChannelAdapter(
        OPENROUTER_BASE,
        (
            ChatCompletions(
                frozenset({"text.generate", "text.structured", "image.understand"}),
                require_parameters=True,
            ),
            OpenRouterImages(),
        ),
        OpenRouter(),
    ),
    "openlux": ChannelAdapter("https://api.openlux.ai/v1", (CHAT, KlingImages()), Openlux()),
    "yunwu": ChannelAdapter("https://yunwu.ai/v1", (CHAT,), Yunwu()),
    # Billing declarations are retained; no inference protocol is installed for these yet.
    "deepseek": ChannelAdapter("https://api.deepseek.com", (), DeepSeek()),
    "aliyun_bailian": ChannelAdapter(
        "https://dashscope.aliyuncs.com/compatible-mode/v1", (), AliyunBailian()
    ),
}

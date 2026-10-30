"""DashScope chat uses non-thinking mode for synchronous and strict JSON calls."""

from keystone.platform.model_access.adapters.inference.protocols import ChatCompletions


class DashScopeChat(ChatCompletions):
    def sdk_settings(self, operation, settings):
        return {
            **settings,
            "extra_body": {**(settings.get("extra_body") or {}), "enable_thinking": False},
        }

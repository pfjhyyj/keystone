"""Verified DashScope request controls; unspecified requests retain non-thinking mode."""

import re

from keystone.platform.model_access.adapters.inference.protocols import ChatCompletions
from keystone.platform.model_access.schemas.errors import AdapterError

# Official Chat Completions and structured-output contracts, verified 2026-11-05.
# https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions
# https://help.aliyun.com/zh/model-studio/qwen-structured-output
HYBRID = re.compile(
    r"(?:qwen3\.[567]-(?:max|plus|flash)|qwen3\.8-(?:max|flash|27b)|"
    r"qwen3-vl-(?:plus|flash)|qwen-flash)(?:-latest|-\d{4}-\d{2}-\d{2}|-\d{4})?"
)
STRUCTURED = re.compile(r"qwen3\.(?:7-(?:max|plus|flash)|8-(?:max|flash))(?:-.*)?")
PLUS = {
    "qwen-plus",
    "qwen-plus-latest",
    "qwen-plus-2025-04-28",
    "qwen-plus-2025-07-14",
    "qwen-plus-2025-12-01",
}
EFFORT = re.compile(r"qwen3\.8-(?:max|flash|27b)(?:-.*)?")


class DashScopeChat(ChatCompletions):
    def reasoning_settings(self, operation, model, reasoning):
        if reasoning is None:
            return {"enable_thinking": False}
        if not isinstance(model, str) or (model not in PLUS and HYBRID.fullmatch(model) is None):
            raise AdapterError(422, "model_invalid_request", "该模型尚未验证显式思考设置")
        if (
            operation == "text.structured"
            and reasoning.mode == "on"
            and not STRUCTURED.fullmatch(model)
        ):
            raise AdapterError(422, "model_invalid_request", "该模型不支持思考与结构化输出组合")
        if reasoning.effort is not None and EFFORT.fullmatch(model) is None:
            raise AdapterError(422, "model_invalid_request", "该模型不支持指定思考强度")
        return {
            "enable_thinking": reasoning.mode == "on",
            **({"reasoning_effort": reasoning.effort} if reasoning.effort is not None else {}),
        }

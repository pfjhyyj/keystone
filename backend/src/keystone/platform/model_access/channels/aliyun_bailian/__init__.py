"""Aliyun Bailian (DashScope): verified thinking controls, native model listing, cloud billing."""

from keystone.platform.model_access.channels.aliyun_bailian import pricing
from keystone.platform.model_access.channels.aliyun_bailian.billing import AliyunBailian
from keystone.platform.model_access.channels.aliyun_bailian.chat import DashScopeChat
from keystone.platform.model_access.channels.aliyun_bailian.discovery import (
    dashscope_models,
)
from keystone.platform.model_access.channels.shared import CHAT, EMBEDDINGS
from keystone.platform.model_access.channels.shared.metering import (
    CHAT_PROFILE,
    EMBEDDING_PROFILE,
)
from keystone.platform.model_access.channels.spec import ChannelField, ChannelSpec

SPEC = ChannelSpec(
    id="aliyun_bailian",
    label="阿里云百炼（DashScope）",
    api_base=pricing.BASE,
    protocols=(DashScopeChat(CHAT.operations), EMBEDDINGS),
    pricing_profiles=(CHAT_PROFILE, EMBEDDING_PROFILE),
    billing=AliyunBailian(),
    models_path="/api/v1/models",
    discover=dashscope_models,
    cloud_billing=True,
    fields=(
        ChannelField(
            name="workspace_id",
            label="百炼业务空间 ID（可选）",
            placeholder="例如 llm-xxxxxxxx",
            pattern="[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?",
            hint=(
                "用已保存的推理 API Key 查询模型列表。留空使用北京区共享域名，"
                "填写后改用该业务空间的北京区专属域名，不改变模型调用地址。"
            ),
        ),
    ),
    reference_prices=pricing.REFERENCE_PRICES,
    reference_applies=pricing.applies,
)

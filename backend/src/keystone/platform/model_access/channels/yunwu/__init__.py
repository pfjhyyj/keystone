"""Yunwu-family relays: key-parameter token logs; unknown compatible sites get chat only."""

from keystone.platform.model_access.channels.shared import RELAY_CHAT, RELAY_IMAGES
from keystone.platform.model_access.channels.shared.metering import (
    OPENAI_IMAGES_PROFILE,
    RELAY_CHAT_PROFILE,
    RELAY_CHAT_PROFILE_V3,
)
from keystone.platform.model_access.channels.spec import ChannelSpec
from keystone.platform.model_access.channels.yunwu.billing import Yunwu

SPEC = ChannelSpec(
    id="yunwu",
    label="Yunwu",
    api_base=Yunwu.api_base,
    protocols=(RELAY_CHAT, RELAY_IMAGES),
    pricing_profiles=(RELAY_CHAT_PROFILE, RELAY_CHAT_PROFILE_V3, OPENAI_IMAGES_PROFILE),
    billing=Yunwu(),
    models_path="/models",
    custom_base=True,
    custom_protocols=(RELAY_CHAT,),
    legacy_sites=("openlux", "yunwu"),
    hint=(
        "Yunwu 类使用 Key 查询参数和分页日志，包含 Yunwu、Openlux 等兼容站点。"
        "默认 https://yunwu.ai/v1，Openlux 填 https://api.openlux.ai/v1；"
        "也可填写兼容站点的 API 地址。此类暂未接入账户余额查询，"
        "更换站点或 Key 时请新建渠道，保留历史查费凭据。"
    ),
)

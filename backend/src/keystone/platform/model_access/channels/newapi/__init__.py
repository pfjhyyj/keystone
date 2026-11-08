"""Standard NewAPI relays at an administrator-provided base; known sites keep their own spec."""

from keystone.platform.model_access.channels.newapi.billing import Newapi
from keystone.platform.model_access.channels.shared import EMBEDDINGS, RELAY_CHAT
from keystone.platform.model_access.channels.shared.metering import (
    EMBEDDING_PROFILE,
    RELAY_CHAT_PROFILE,
)
from keystone.platform.model_access.channels.spec import ChannelSpec

SPEC = ChannelSpec(
    id="newapi",
    label="NewAPI",
    api_base=Newapi.api_base,
    protocols=(RELAY_CHAT, EMBEDDINGS),
    pricing_profiles=(RELAY_CHAT_PROFILE, EMBEDDING_PROFILE),
    billing=Newapi(),
    models_path="/models",
    custom_base=True,
    legacy_sites=("openlux", "yunwu"),
    hint=(
        "填写标准 NewAPI 的 HTTPS API 地址，默认 https://newapiai.com/v1，可包含路径前缀。"
        "普通 Key 用于模型列表、调用费用核对和额度查询。上游允许时同时查询账户剩余额度，"
        "与当前 Key 的额度分开展示；Token 不限额仍受账户余额限制。"
        "更换站点或 Key 时请新建渠道，保留历史查费凭据。"
    ),
)

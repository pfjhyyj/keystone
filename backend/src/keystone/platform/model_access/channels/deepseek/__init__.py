"""DeepSeek retains its billing declaration without an inference protocol."""

from keystone.platform.model_access.channels.deepseek.billing import DeepSeek
from keystone.platform.model_access.channels.spec import ChannelSpec

SPEC = ChannelSpec(
    id="deepseek",
    label="DeepSeek",
    api_base=DeepSeek.api_base,
    protocols=(),
    billing=DeepSeek(),
    creatable=False,
)

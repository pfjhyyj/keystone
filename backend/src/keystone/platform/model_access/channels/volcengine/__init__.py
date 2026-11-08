"""Volcengine Ark: chat with service-tier metering and cloud account billing."""

from dataclasses import replace

from keystone.platform.model_access.channels.shared import CHAT
from keystone.platform.model_access.channels.spec import ChannelSpec
from keystone.platform.model_access.channels.volcengine import pricing
from keystone.platform.model_access.channels.volcengine.billing import Volcengine

SPEC = ChannelSpec(
    id="volcengine",
    label="火山方舟（豆包）",
    api_base=pricing.BASE,
    protocols=(replace(CHAT, pricing_meter="volcengine_chat.v2"),),
    pricing_profiles=(pricing.PROFILE,),
    pricing_context_defaults=pricing.CONTEXT_DEFAULTS,
    billing=Volcengine(),
    models_path="/models",
    cloud_billing=True,
    reference_prices=pricing.REFERENCE_PRICES,
    reference_applies=pricing.applies,
)

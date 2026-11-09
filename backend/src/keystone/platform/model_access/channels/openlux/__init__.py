"""Openlux: a fixed Yunwu-family relay site with the native Kling image task protocol."""

from keystone.platform.model_access.channels.openlux.billing import Openlux
from keystone.platform.model_access.channels.openlux.kling import (
    BASE,
    PROFILE,
    KlingImages,
)
from keystone.platform.model_access.channels.shared import RELAY_CHAT, RELAY_IMAGES
from keystone.platform.model_access.channels.shared.metering import (
    OPENAI_IMAGES_PROFILE,
    RELAY_CHAT_PROFILE,
    RELAY_CHAT_PROFILE_V3,
)
from keystone.platform.model_access.channels.spec import ChannelSpec

SPEC = ChannelSpec(
    id="openlux",
    label="Openlux",
    api_base=Openlux.api_base,
    protocols=(
        RELAY_CHAT,
        RELAY_IMAGES,
        KlingImages(
            allowed_targets=frozenset({("openlux", BASE), ("newapi", BASE), ("yunwu", BASE)})
        ),
    ),
    pricing_profiles=(RELAY_CHAT_PROFILE, RELAY_CHAT_PROFILE_V3, OPENAI_IMAGES_PROFILE, PROFILE),
    billing=Openlux(),
    models_path="/models",
    creatable=False,
    display_as="yunwu",
)

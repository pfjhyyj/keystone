"""Focused contracts for channel descriptors, registration and relay resolution."""

import pytest
from pydantic import TypeAdapter, ValidationError

from keystone.platform.model_access import channels
from keystone.platform.model_access.channels.shared import EMBEDDINGS, RELAY_CHAT
from keystone.platform.model_access.channels.spec import ChannelSpec
from keystone.platform.model_access.schemas.types import Provider


def test_channel_options_descriptors_and_legacy_capabilities():
    options = channels.channel_options()
    descriptors = {item["id"]: item for item in options["channels"]}
    expected = {
        "openrouter": ("OpenRouter", "key", True, False),
        "newapi": ("NewAPI", "none", True, False),
        "yunwu": ("Yunwu", "none", False, False),
        "aliyun_bailian": ("阿里云百炼（DashScope）", "cloud", True, True),
        "volcengine": ("火山方舟（豆包）", "cloud", True, True),
        "openlux": ("Openlux", "none", False, False),
        "deepseek": ("DeepSeek", "none", False, False),
    }
    assert descriptors.keys() == expected.keys()
    for identifier, descriptor in descriptors.items():
        assert set(descriptor) == {
            "id",
            "label",
            "display_as",
            "creatable",
            "default_api_base",
            "custom_base",
            "balance",
            "bills",
            "billing_credentials",
            "fields",
            "hint",
        }
        assert (
            tuple(descriptor[key] for key in ("label", "billing_credentials", "balance", "bills"))
            == expected[identifier]
        )
        assert descriptor["default_api_base"] == channels.CHANNELS[identifier].api_base
        fields = descriptor["fields"]
        if identifier == "aliyun_bailian":
            assert len(fields) == 1 and fields[0]["name"] == "workspace_id"
            assert set(fields[0]) == {"name", "label", "hint", "placeholder", "pattern"}
        else:
            assert fields == []
    creatable = [item["id"] for item in options["channels"] if item["creatable"]]
    assert creatable == ["openrouter", "newapi", "yunwu", "aliyun_bailian", "volcengine"]
    assert sorted(options) == ["channels", "legacy_sites"]
    assert options["legacy_sites"] == [
        {
            "api_base": base,
            "providers": ["newapi", "yunwu"],
            "display_as": "yunwu",
            "balance": False,
            "bills": False,
            "billing_credentials": "none",
        }
        for base in ("https://api.openlux.ai/v1", "https://yunwu.ai/v1")
    ]
    for identifier in ("openlux", "deepseek"):
        assert descriptors[identifier]["creatable"] is False
        assert TypeAdapter(Provider).validate_python(identifier) == identifier
    assert descriptors["openlux"]["display_as"] == "yunwu"


def test_new_spec_is_accepted_by_provider_and_routing(monkeypatch):
    validator = TypeAdapter(Provider)
    spec = ChannelSpec(
        id="nebula_test",
        label="星云试验渠道",
        api_base="https://nebula.invalid/v1",
        protocols=(RELAY_CHAT, EMBEDDINGS),
        billing=channels.CHANNELS["newapi"].billing,
    )
    with pytest.raises(ValidationError, match="Unknown model provider"):
        validator.validate_python(spec.id)
    monkeypatch.setitem(channels.CHANNELS, spec.id, spec)
    assert validator.validate_python(spec.id) == spec.id
    assert channels.channel_adapter(spec.id) is spec
    assert (spec.id, spec.api_base, spec) in list(channels.channel_targets())


def test_known_sites_take_priority_and_unknown_relays_keep_their_protocols():
    for provider in ("newapi", "yunwu"):
        for site in ("openlux", "yunwu"):
            known = channels.CHANNELS[site]
            assert channels.channel_adapter(provider, known.api_base) is known
        base = "https://relay.invalid/prefix/v1"
        custom = channels.channel_adapter(provider, base)
        assert custom.id == provider and custom.api_base == base
        assert custom.protocols == (
            (RELAY_CHAT, EMBEDDINGS) if provider == "newapi" else (RELAY_CHAT,)
        )
        for operation in ("text.generate", "text.structured", "image.understand"):
            assert custom.supports(operation, "sync")
        assert custom.supports("text.embed", "sync") is (provider == "newapi")
        assert not custom.supports("image.generate", "sync")
        assert not custom.supports("image.generate", "async", model="kling-v2-1")

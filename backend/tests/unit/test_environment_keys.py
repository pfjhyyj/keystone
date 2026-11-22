import pytest

from keystone.platform.environments.schemas.keys import EnvironmentKey, environment_keys


def test_business_keys_are_namespaced_unique_and_use_known_kinds():
    keys = environment_keys(
        (("reports", (EnvironmentKey("reports.portal", "报表门户", ("chromium",)),)),)
    )
    assert set(keys) == {"default", "reports.portal"}

    for invalid in (
        EnvironmentKey("default", "重名", ("chromium",)),
        EnvironmentKey("other.subscription", "越界", ("chromium",)),
        EnvironmentKey("reports.portal", " ", ("chromium",)),
        EnvironmentKey("reports.portal", "报表门户", ()),
        EnvironmentKey("reports.portal", "报表门户", ("android",)),
    ):
        with pytest.raises(ValueError, match="environment key"):
            environment_keys((("reports", (invalid,)),))

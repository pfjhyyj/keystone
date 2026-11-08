"""Send controls normalize untrusted headers and join shared deployment identity."""

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import pytest
from pydantic import ValidationError

from keystone.core.config import Settings
from keystone.core.worker_identity import config_digest
from keystone.platform.model_access.channels.shared.limits import retry_after


def test_retry_after_bounds_seconds_dates_and_untrusted_values():
    now = datetime(2026, 10, 24, tzinfo=UTC)
    for value, expected in (
        (None, None),
        ("", None),
        ("²", None),
        ("bad-date", None),
        ("9" * 129, None),
        ("0", 1),
        ("7", 7),
        ("99999999999999999", 3600),
        (format_datetime(now + timedelta(seconds=12)), 12),
        (format_datetime(now - timedelta(seconds=1)), 1),
        (format_datetime(now + timedelta(days=30)), 3600),
    ):
        assert retry_after(value, now=now) == expected


@pytest.mark.parametrize(
    "field,value",
    [
        ("model_send_interval_ms", -1),
        ("model_send_interval_ms", 60001),
        ("model_cooldown_seconds", 0),
        ("model_cooldown_seconds", 301),
        ("model_cooldown_max_seconds", 0),
        ("model_cooldown_max_seconds", 3601),
    ],
)
def test_invalid_dispatch_configuration_is_rejected(field, value):
    with pytest.raises(ValidationError):
        Settings(config_file=None, **{field: value})


def test_dispatch_settings_share_the_worker_configuration_digest():
    original = Settings(config_file=None)
    assert original.model_send_interval_ms == 0
    for field, value in (
        ("model_send_interval_ms", 10),
        ("model_cooldown_seconds", 6),
        ("model_cooldown_max_seconds", 301),
    ):
        assert config_digest(Settings(config_file=None, **{field: value})) != config_digest(
            original
        )
    with pytest.raises(ValidationError):
        Settings(config_file=None, model_cooldown_seconds=10, model_cooldown_max_seconds=5)

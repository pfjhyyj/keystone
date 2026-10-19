from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator

from keystone.core.schemas import Input

from .types import Label, Provider


class ChannelEdit(Input):
    name: Label
    api_base: str = Field(max_length=512)
    api_key: SecretStr | None = Field(default=None, min_length=1, max_length=4096, repr=False)
    billing_api_key: SecretStr | None = Field(
        default=None, min_length=1, max_length=4096, repr=False
    )
    clear_billing_api_key: bool = False
    upstream_account_ref: Label | None = None
    is_active: bool = True

    @field_validator("api_key", "billing_api_key")
    @classmethod
    def safe_secret(cls, value):
        if value is not None and any(c.isspace() or ord(c) < 32 for c in value.get_secret_value()):
            raise ValueError("API keys cannot contain whitespace or control characters")
        return value

    @model_validator(mode="after")
    def billing_update(self):
        if self.clear_billing_api_key and self.billing_api_key is not None:
            raise ValueError("Cannot replace and clear a billing key together")
        return self

    @field_validator("api_base")
    @classmethod
    def safe_url(cls, value):
        url = urlsplit(value)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or any(c.isspace() or ord(c) < 32 for c in value)
            or "\\" in value
            or "%" in value
            or ".." in url.path
        ):
            raise ValueError("Use an HTTPS service URL without credentials, query or fragment")
        _ = url.port  # Reject malformed ports before storing a configuration.
        return value.rstrip("/")


class ChannelCreate(ChannelEdit):
    provider: Provider
    api_key: SecretStr = Field(min_length=1, max_length=4096, repr=False)


def channel_view(row):
    values = {
        key: getattr(row, key)
        for key in (
            "id",
            "name",
            "provider",
            "api_base",
            "upstream_account_ref",
            "is_active",
            "balance_snapshot",
            "balance_checked_at",
            "balance_last_attempt_at",
            "balance_check_status",
            "created_at",
            "created_by",
            "updated_at",
            "updated_by",
        )
    }
    return {
        **values,
        "has_api_key": bool(getattr(row, "api_key_encrypted", None)),
        "has_billing_api_key": bool(getattr(row, "billing_api_key_encrypted", None)),
    }

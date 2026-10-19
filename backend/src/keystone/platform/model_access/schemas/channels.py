from urllib.parse import urlsplit

from pydantic import Field, field_validator

from keystone.core.schemas import Input

from .types import Label, Provider, Reference


class ChannelEdit(Input):
    name: Label
    api_base: str = Field(max_length=512)
    credential_ref: Reference
    billing_credential_ref: Reference | None = None
    upstream_account_ref: Label | None = None
    is_active: bool = True

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


def channel_view(row):
    values = {
        key: getattr(row, key)
        for key in (
            "id",
            "name",
            "provider",
            "api_base",
            "credential_ref",
            "billing_credential_ref",
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
    return values

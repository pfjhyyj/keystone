from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field, SecretStr, field_validator

from keystone.core.schemas import Input

Name = Field(min_length=1, max_length=128)
WEBHOOK_HOSTS = {"open.feishu.cn", "open.larksuite.com"}


class WebhookCredential(Input):
    url: SecretStr
    secret: SecretStr | None = None

    @field_validator("url")
    @classmethod
    def feishu_hook(cls, value):
        parts = urlsplit(value.get_secret_value())
        if (
            parts.scheme != "https"
            or parts.hostname not in WEBHOOK_HOSTS
            or parts.port is not None
            or parts.username
            or parts.query
            or parts.fragment
            or not parts.path.startswith("/open-apis/bot/v2/hook/")
            or len(parts.path) <= len("/open-apis/bot/v2/hook/")
        ):
            raise ValueError("Use the group bot webhook address from Feishu")
        return value


class ChannelCreate(Input):
    name: str = Name
    provider: Literal["feishu_webhook"]
    credential: WebhookCredential


class ChannelEdit(Input):
    name: str = Name
    is_active: bool
    # Omitted keeps the stored credential; a value replaces it as a whole.
    credential: WebhookCredential | None = None


class DestinationCreate(Input):
    key: str = Field(pattern=r"^[a-z][a-z0-9_.]{0,63}$")
    channel_id: UUID
    name: str = Name
    external_id: str | None = Field(None, min_length=1, max_length=128)


class DestinationEdit(Input):
    name: str = Name
    is_active: bool

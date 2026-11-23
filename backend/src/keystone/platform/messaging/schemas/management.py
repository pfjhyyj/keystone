from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field, SecretStr, field_validator

from keystone.core.schemas import Input

Name = Field(min_length=1, max_length=128)
Key = Field(pattern=r"^[a-z][a-z0-9_.]{0,63}$")
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


class AppCredential(Input):
    # The app ID identifies the app and is fixed with the channel; only the secret rotates.
    app_secret: SecretStr = Field(min_length=1, max_length=256)


class WebhookChannelCreate(Input):
    name: str = Name
    provider: Literal["feishu_webhook"]
    credential: WebhookCredential
    # A group bot reaches exactly one group, so its destination is created with it.
    destination_key: str = Key


class AppChannelCreate(Input):
    name: str = Name
    provider: Literal["feishu_app"]
    app_id: str = Field(pattern=r"^cli_[A-Za-z0-9]{1,60}$")
    credential: AppCredential


ChannelCreate = Annotated[WebhookChannelCreate | AppChannelCreate, Field(discriminator="provider")]


class ChannelEdit(Input):
    name: str = Name
    is_active: bool
    # Omitted keeps the stored credential; a value replaces it and must match the provider.
    credential: WebhookCredential | AppCredential | None = None


class DestinationCreate(Input):
    key: str = Key
    channel_id: UUID
    name: str = Name
    # The group chat ID for app channels, chosen from the groups the bot is in.
    external_id: str | None = Field(None, pattern=r"^oc_[A-Za-z0-9]{1,124}$")


class DestinationEdit(Input):
    name: str = Name
    is_active: bool

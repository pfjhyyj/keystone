from datetime import datetime

from pydantic import Field, field_validator

from keystone.core.schemas import Input

from .types import Name


class TokenCreate(Input):
    name: Name
    permissions: set[str] = Field(max_length=100)
    expires_at: datetime

    @field_validator("expires_at")
    @classmethod
    def aware(cls, value):
        if value.tzinfo is None:
            raise ValueError("Use a timezone-aware expiry")
        return value


def token_view(token):
    fields = ("id", "user_id", "name", "permissions", "expires_at", "revoked_at", "created_at")
    return {key: getattr(token, key) for key in fields}

from typing import Literal

from pydantic import Field, model_validator

from keystone.core.schemas import Input
from keystone.core.storage_config import StorageKey

from .types import Name


class Upload(Input):
    storage_key: StorageKey | None = None
    name: Name
    content_type: str = Field(
        "application/octet-stream",
        max_length=128,
        pattern=r"^[a-zA-Z0-9!#$&^_.+-]+/[a-zA-Z0-9!#$&^_.+-]+$",
    )
    expected_size: int = Field(ge=0)
    checksum_sha256: str | None = Field(None, pattern=r"^[0-9a-f]{64}$")
    request_key: str | None = Field(None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def checksum_required(self):
        if self.request_key and not self.checksum_sha256:
            raise ValueError("Idempotent uploads require a checksum")
        return self


class Edit(Input):
    name: Name
    status: Literal["active", "disabled"]


def view(file):
    keys = (
        "id",
        "name",
        "owner_id",
        "content_type",
        "storage_key",
        "size_bytes",
        "checksum_sha256",
        "status",
        "deleted_at",
        "created_at",
        "created_by",
        "updated_at",
        "updated_by",
    )
    return {key: getattr(file, key) for key in keys}

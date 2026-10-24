"""Trusted per-caller temporary limits; never accepted as file-upload HTTP parameters."""

from pydantic import ConfigDict, Field

from keystone.core.schemas import Input


class TransferScope(Input):
    model_config = ConfigDict(frozen=True)
    owner: str = Field(pattern=r"^[a-z][a-z0-9:._-]{0,159}$")
    max_temporary_bytes: int = Field(strict=True, ge=0, le=32 * 1024**4)

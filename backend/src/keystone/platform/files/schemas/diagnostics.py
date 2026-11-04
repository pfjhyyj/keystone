"""Reference metadata does not grant access to file content."""

from uuid import UUID

from pydantic import Field

from keystone.core.schemas import Input


class ReferenceDiagnostic(Input):
    """Trusted metadata projection; the upper use case emits its own public DTO."""

    file_id: UUID
    slot: str = Field(min_length=1, max_length=100)
    checksum_sha256: str | None = Field(None, max_length=64)
    size_bytes: int | None = Field(None, strict=True, ge=0, le=2**63 - 1)
    content_type: str = Field(max_length=128)


class ReferenceDiagnostics(Input):
    items: list[ReferenceDiagnostic] = []
    truncated: bool = False
    next_offset: int | None = None

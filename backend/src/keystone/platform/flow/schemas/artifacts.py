"""Small managed-file results returned by Flow, never raw generation buffers."""

from uuid import UUID

from pydantic import Field

from keystone.core.schemas import Input


class ImageFile(Input):
    file_id: UUID


class ImageResult(Input):
    invocation_id: UUID
    files: list[ImageFile] = Field(min_length=1, max_length=2)

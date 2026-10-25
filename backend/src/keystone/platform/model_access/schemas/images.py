"""File-based media requests; bytes and supplier locations stay internal."""

from dataclasses import dataclass
from datetime import timedelta
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from keystone.core.schemas import Input
from keystone.platform.model_access.schemas.invocations import TextRequest
from keystone.platform.model_access.schemas.types import Key

MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_INPUT_IMAGE_BYTES = 8 * 1024 * 1024
MAX_RELAY_INPUT_BYTES = 32 * 1024 * 1024
MAX_IMAGE_PIXELS = 16 * 1024 * 1024


class ImageInput(Input):
    file_id: UUID
    detail: Literal["auto", "low", "high"] = "auto"


class VisionRequest(TextRequest):
    images: list[ImageInput] = Field(min_length=1, max_length=4)


MAX_OUTPUT_IMAGE_BYTES = 8 * 1024 * 1024


class ImageGenerateRequest(Input):
    prompt: str = Field(min_length=1, max_length=16000)
    n: int = Field(default=1, strict=True, ge=1, le=2)
    resolution: Literal["1K", "2K", "4K"] = "1K"
    output_format: Literal["png", "jpeg", "webp"] = "png"
    aspect_ratio: Literal["1:1", "16:9", "9:16", "4:3", "3:4"] | None = None
    quality: Literal["low", "medium", "high"] | None = None


class ImageEditRequest(ImageGenerateRequest):
    images: list[ImageInput] = Field(min_length=1, max_length=9)


class ImageGenerateInvoke(ImageGenerateRequest):
    model_key: Key


class ImageEditInvoke(ImageEditRequest):
    model_key: Key


class VisionInvoke(VisionRequest):
    model_key: Key


class ImageFile(Input):
    file_id: UUID


class ImageResult(Input):
    invocation_id: UUID
    files: list[ImageFile] = Field(min_length=1, max_length=2)


class ImageOutput(Input):
    artifact_key: str = Field(pattern=r"^model\.[0-9a-f-]{36}\.[01]$")
    checksum_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(strict=True, gt=0, le=MAX_OUTPUT_IMAGE_BYTES)
    content_type: Literal["image/png", "image/jpeg", "image/webp"]


class ImageTaskRequest(Input):
    prompt: str = Field(min_length=1, max_length=2500)
    n: int = Field(default=1, strict=True, ge=1, le=2)
    resolution: Literal["1K", "2K"] = "1K"


class ImageTask(Input):
    invocation_id: UUID


class ImagePending(ImageTask):
    state: Literal["pending"] = "pending"
    retry_after_seconds: int = Field(default=5, strict=True, ge=1, le=60)


class ImageTaskReceipt(Input):
    format_version: Literal[1] = 1
    kind: Literal["task"] = "task"
    protocol: Literal["openlux.kling.images"] = "openlux.kling.images"
    operation: Literal["image.generate"] = "image.generate"
    task_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    count: int = Field(strict=True, ge=1, le=2)
    submitted_at: AwareDatetime
    deadline_at: AwareDatetime
    next_query_at: AwareDatetime
    queries: int = Field(default=0, strict=True, ge=0, le=60)

    @model_validator(mode="after")
    def bounded_lifetime(self):
        if not timedelta(0) < self.deadline_at - self.submitted_at <= timedelta(seconds=300):
            raise ValueError("Invalid image task deadline")
        if not self.submitted_at <= self.next_query_at <= self.deadline_at:
            raise ValueError("Invalid image query time")
        return self


class ImageDownloadReceipt(Input):
    format_version: Literal[1] = 1
    kind: Literal["download"] = "download"
    operation: Literal["image.generate", "image.edit"]
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    sources_encrypted: str = Field(min_length=1, max_length=12000, repr=False)
    count: int = Field(strict=True, ge=1, le=2)
    deadline_at: AwareDatetime


class ImageManifest(Input):
    format_version: Literal[1] = 1
    kind: Literal["images"] = "images"
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    operation: Literal["image.generate", "image.edit"]
    outputs: list[ImageOutput] = Field(min_length=1, max_length=2)
    task: ImageTaskReceipt | None = None
    download: ImageDownloadReceipt | None = None

    @model_validator(mode="after")
    def coherent_task(self):
        if self.task and (
            self.operation != self.task.operation
            or self.request_digest != self.task.request_digest
            or len(self.outputs) > self.task.count
        ):
            raise ValueError("Image manifest differs from original task")
        if self.download and (
            self.task is not None
            or self.operation != self.download.operation
            or self.request_digest != self.download.request_digest
            or len(self.outputs) != self.download.count
        ):
            raise ValueError("Image manifest differs from download receipt")
        return self


# Process-local result only. FlowContext consumes bytes before returning a checkpoint value.


@dataclass(frozen=True)
class ImageBatch:
    manifest: ImageManifest
    contents: tuple[bytes, ...]


@dataclass(frozen=True)
class ImageSources:
    """Process-local locations, sealed by the host before any download."""

    urls: tuple[str, ...]
    operation: Literal["image.generate", "image.edit"]
    request_digest: str

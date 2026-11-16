"""Bounded, explicitly recorded diagnostics for one durable step attempt."""

import hashlib
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, JsonValue, StringConstraints

from keystone.core.schemas import Input

MAX_DETAIL_BYTES = 64 * 1024
MAX_PREVIEW_BYTES = 4 * 1024 * 1024
MAX_ATTEMPTS = 50
MAX_ATTACHMENTS = 8
ATTACHMENT_TYPES = frozenset({"text/plain", "application/json", "image/jpeg", "image/png"})


def attempt_prefix(workflow_id: str, step_id: int) -> str:
    digest = hashlib.sha256(workflow_id.encode()).hexdigest()[:32]
    return f"diagnostic.step.{digest}.{step_id}."


class StepAttachment(Input):
    label: Annotated[str, StringConstraints(min_length=1, max_length=80)]
    file_id: UUID


class StepFailure(Input):
    code: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,79}$")]
    message: Annotated[str, StringConstraints(min_length=1, max_length=300)]


class StepAttempt(Input):
    attempt: int = Field(ge=1, le=2**31 - 1)
    label: Annotated[str, StringConstraints(min_length=1, max_length=80)]
    kind: Literal["original", "correction"] = "original"
    input: dict[str, JsonValue] | None = None
    output: JsonValue = None
    error: StepFailure | None = None
    attachments: list[StepAttachment] = Field(default_factory=list, max_length=MAX_ATTACHMENTS)


class StepAttempts(Input):
    items: list[StepAttempt]
    truncated: bool = False

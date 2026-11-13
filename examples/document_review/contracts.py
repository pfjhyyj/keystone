"""Lightweight public contracts and deterministic business acceptance."""

from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator

from keystone.core.schemas import Input
from keystone.platform.flow.schemas.definitions import FlowDefinition, ModelRole
from keystone.platform.flow.schemas.packages import FlowPackage, PackageResource

SOURCE_BYTES = 16384
PREVIEW_CHARACTERS = 512
EXCERPT_CHARACTERS = 2048


class ReviewInput(Input):
    source_file_id: UUID


class Judgment(Input):
    source_file_id: UUID = Field(description="The source_file_id returned by inspect_document")
    characters: int = Field(ge=0, le=SOURCE_BYTES, description="Exact source character count")
    verdict: Literal["reviewed", "needs_attention"]
    summary: str = Field(min_length=1, max_length=2000)

    @field_validator("summary")
    @classmethod
    def nonblank_summary(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("Summary must contain non-whitespace text")
        return value


def accept_judgment(value: Judgment, *, source_file_id: str, characters: int) -> Judgment:
    """Cross-field/source checks belong to business code, before any publication."""
    if value.source_file_id != UUID(source_file_id):
        raise ValueError("source_file_id must match the inspected source")
    if value.characters != characters:
        raise ValueError("characters must match the inspected source character count")
    return value


class ReviewOutput(Judgment):
    report_file_id: UUID


class DocumentSummary(Input):
    status: Literal["succeeded"] = "succeeded"
    summary: str = Field(max_length=240)
    source_file_id: UUID
    characters: int = Field(ge=0, le=SOURCE_BYTES)
    preview: str = Field(max_length=PREVIEW_CHARACTERS)
    truncated: bool


class DocumentExcerpt(Input):
    status: Literal["succeeded"] = "succeeded"
    summary: str = Field(max_length=240)
    source_file_id: UUID
    offset: int = Field(ge=0)
    text: str = Field(max_length=EXCERPT_CHARACTERS)
    next_offset: int = Field(ge=0)
    truncated: bool


definition = FlowDefinition(
    revision="1",
    key="document.review",
    name="文本文件审查",
    description="读取小型 UTF-8 文本，由 Agent 审查并保存通过验收的报告。",
    input_model=ReviewInput,
    output_model=ReviewOutput,
    entrypoint="keystone.document.review",
    # Recovery may reuse the report through existing file inspection/management checks.
    required_permissions=("model:invoke",),
    file_fields=("source_file_id",),
    output_file_fields=("report_file_id",),
    model_roles=(ModelRole("review", "审查"),),
    timeout_seconds=180,
)


PACKAGE = FlowPackage(
    key="document-tools",
    host_api_version=1,
    definitions=(definition,),
    execution_modules=("document_review.workflow",),
    execution_packages=("document_review",),
    required_resources=(
        PackageResource(package="document_review", path="skills/review-text/SKILL.md"),
    ),
)

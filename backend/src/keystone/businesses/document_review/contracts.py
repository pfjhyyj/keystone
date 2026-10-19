from typing import Literal
from uuid import UUID

from pydantic import Field

from keystone.core.schemas import Input
from keystone.platform.flow.catalog import Definition
from keystone.platform.model_access.schemas import Key


class ReviewInput(Input):
    source_file_id: UUID
    model_key: Key


class Judgment(Input):
    verdict: Literal["reviewed", "needs_attention"]
    summary: str = Field(min_length=1, max_length=2000)


class ReviewOutput(Judgment):
    report_file_id: UUID


definition = Definition(
    key="document.review",
    name="文本文件审查",
    description="读取小型 UTF-8 文本，由 Agent 审查并保存报告。",
    input_type=ReviewInput,
    output_type=ReviewOutput,
    entrypoint="keystone.document.review",
    # Stable upload replay uses the file service's existing inspect/manage checks too.
    required_permissions=(
        "file:content_read",
        "file:create",
        "file:inspect",
        "file:manage",
        "model:invoke",
    ),
    file_fields=("source_file_id",),
    output_file_fields=("report_file_id",),
    model_field="model_key",
)

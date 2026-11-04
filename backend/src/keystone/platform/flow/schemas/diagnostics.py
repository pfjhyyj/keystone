"""Explicit Run diagnostic response; provider evidence and checkpoints stay private."""

import re
from typing import Literal
from uuid import UUID

from pydantic import Field

from keystone.core.schemas import Input
from keystone.platform.model_access.schemas.diagnostics import InvocationDiagnostics

DiagnosticSection = Literal["all", "models", "files"]


class FlowFileDiagnostic(Input):
    file_id: UUID
    slot: str = Field(min_length=1, max_length=100)
    invocation_id: UUID | None = None


class FlowFileDiagnostics(Input):
    items: list[FlowFileDiagnostic] = []
    truncated: bool = False
    next_offset: int | None = None


def source_invocation(slot):
    match = re.fullmatch(r"artifact\.model\.([0-9a-f-]{36})\.[0-3]", slot)
    if match:
        try:
            return UUID(match[1])
        except ValueError:
            pass
    return None


class Diagnostics(Input):
    run_id: UUID
    models: InvocationDiagnostics | None = None
    files: FlowFileDiagnostics | None = None

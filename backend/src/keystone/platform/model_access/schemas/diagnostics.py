"""Bounded public correlation, without billing or provider evidence."""

from typing import Literal
from uuid import UUID

from pydantic import Field

from keystone.core.schemas import Input
from keystone.platform.model_access.schemas.invocations import InvocationContext
from keystone.platform.model_access.schemas.operations import Operation

SAFE_ERRORS = frozenset(
    {
        "model_invalid_request",
        "model_authentication_failed",
        "model_insufficient_credits",
        "model_access_denied",
        "upstream_model_not_found",
        "model_rate_limited",
        "model_upstream_error",
        "model_timeout",
        "model_interrupted",
        "model_response_unknown",
        "model_invalid_output",
        "model_task_failed",
        "model_task_timeout",
        "model_result_unavailable",
        "model_configuration_changed",
        "flow_cancelled",
        "flow_identity_unavailable",
        "permission_denied",
        "authentication_required",
    }
)


class InvocationDiagnostic(Input):
    invocation_id: UUID
    operation: Operation
    status: Literal["running", "succeeded", "failed", "unknown"]
    model_role: str | None = None
    node_key: str | None = None
    agent_key: str | None = None
    agent_call_id: UUID | None = None
    workflow_id: str | None = None
    step_id: int | None = None
    error_code: str | None = None


class InvocationDiagnostics(Input):
    available: bool
    items: list[InvocationDiagnostic] = []
    truncated: bool = False
    next_offset: int | None = None


class ImageSource(Input):
    """Trusted file metadata to match against an authorized original image Manifest."""

    file_id: UUID
    invocation_id: UUID
    artifact_key: str = Field(min_length=1, max_length=80)
    checksum_sha256: str | None = Field(None, max_length=64)
    size_bytes: int | None = Field(None, strict=True, ge=0, le=2**63 - 1)
    content_type: str = Field(max_length=128)


def diagnostic(row):
    context = row.flow_context if isinstance(row.flow_context, dict) else {}
    values = {key: context[key] for key in InvocationContext.model_fields if key in context}
    try:
        values = InvocationContext.model_validate(values).model_dump(exclude={"flow_run_id"})
    except ValueError:
        values = {}  # Historical invalid observations never expose arbitrary JSON.
    return InvocationDiagnostic(
        invocation_id=row.id,
        operation=row.operation,
        status=row.status,
        error_code=(row.error_code if row.error_code in SAFE_ERRORS else "model_response_unknown")
        if row.error_code
        else None,
        **values,
    )

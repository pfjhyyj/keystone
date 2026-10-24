"""Observational positions never become authority or leak provider JSON."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from keystone.core.errors import ServiceError
from keystone.platform.flow.schemas.diagnostics import source_invocation
from keystone.platform.model_access.adapters.inference import kling
from keystone.platform.model_access.schemas.diagnostics import diagnostic
from keystone.platform.model_access.schemas.invocations import InvocationContext, Selection
from keystone.platform.model_access.services.image_tasks import ImageTaskService


def context(**changes):
    return InvocationContext(
        flow_run_id=uuid4(), node_key="generate", model_role="generate", **changes
    )


def test_original_task_authority_ignores_poll_step_and_accepts_missing_historical_position():
    selected = Selection(
        model_entry_id=uuid4(),
        channel_id=uuid4(),
        provider_model="kling-v1-5",
        api_base=kling.BASE,
        provider="openlux",
        operation="image.generate",
        delivery="async",
    )
    original = context(workflow_id="submit-workflow", step_id=2)
    row = SimpleNamespace(
        operation="image.generate",
        flow_context=original.model_dump(mode="json", exclude_none=True),
        model_entry_id=selected.model_entry_id,
        channel_id=selected.channel_id,
        provider_model=selected.provider_model,
    )
    current = original.model_copy(update={"workflow_id": "poll-workflow", "step_id": 8})
    ImageTaskService.require_original(row, selected, current)
    assert row.flow_context["workflow_id"] == "submit-workflow" and row.flow_context["step_id"] == 2
    row.flow_context = original.authority()
    ImageTaskService.require_original(row, selected, current)
    for changes in (
        {"flow_run_id": uuid4()},
        {"node_key": "other"},
        {"model_role": "edit"},
        {"agent_key": "other"},
        {"agent_call_id": uuid4()},
    ):
        with pytest.raises(ServiceError):
            ImageTaskService.require_original(row, selected, current.model_copy(update=changes))


def test_position_and_projection_are_bounded_and_historical_omissions_remain_empty():
    original = context()
    assert original.workflow_id is None and original.step_id is None
    for change in (
        {"step_id": True},
        {"step_id": -1},
        {"step_id": 2**31},
        {"workflow_id": "x" * 513},
    ):
        with pytest.raises(ValidationError):
            InvocationContext.model_validate({**original.model_dump(), **change})
    row = SimpleNamespace(
        id=uuid4(),
        operation="text.generate",
        status="unknown",
        error_code="private exception",
        flow_context={**original.authority(), "secret": "private JSON"},
    )
    value = diagnostic(row).model_dump(mode="json")
    assert value["workflow_id"] is None and value["step_id"] is None
    assert value["error_code"] == "model_response_unknown"
    assert "private" not in str(value)
    row.flow_context = {"node_key": {"private": "exception"}}
    assert diagnostic(row).node_key is None
    identifier = uuid4()
    assert source_invocation(f"artifact.model.{identifier}.0") == identifier
    for slot in (
        "output",
        "input",
        "artifact.report",
        f"artifact.model.{identifier}.99",
        f"artifact.model.{identifier}.0.extra",
    ):
        assert source_invocation(slot) is None


def test_budget_diagnostics_preserve_full_integer_precision():
    from keystone.platform.flow.schemas.budgets import BudgetCounters, FlowLimits
    from keystone.platform.flow.schemas.diagnostics import BudgetDiagnostic

    values = BudgetDiagnostic.from_values(
        FlowLimits(input_tokens=2**63 - 1), BudgetCounters(input_tokens=2**127 - 1)
    ).model_dump(mode="json")
    assert values["limits"]["input_tokens"] == str(2**63 - 1)
    assert values["counters"]["input_tokens"] == str(2**127 - 1)
    assert all(isinstance(value, str) for value in values["limits"].values())
    assert all(isinstance(value, str) for value in values["counters"].values())


async def test_source_confirmation_is_bounded_before_any_query():
    from unittest.mock import AsyncMock

    from keystone.platform.model_access.schemas.diagnostics import ImageSource
    from keystone.platform.model_access.services.invocations import InvocationService

    source = ImageSource(
        file_id=uuid4(), invocation_id=uuid4(), artifact_key="candidate", content_type="image/png"
    )
    session = SimpleNamespace(execute=AsyncMock())
    auth = SimpleNamespace(permissions={"model_invocation:read"})
    with pytest.raises(ValueError, match="Too many"):
        await InvocationService(None, None).image_sources_in_transaction(
            session, auth, uuid4(), [source] * 101
        )
    session.execute.assert_not_called()


def test_public_file_diagnostic_cannot_inherit_internal_proof_fields():
    from keystone.platform.files.schemas.diagnostics import ReferenceDiagnostic
    from keystone.platform.flow.schemas.diagnostics import FlowFileDiagnostic

    assert {
        "checksum_sha256",
        "size_bytes",
        "content_type",
    } <= ReferenceDiagnostic.model_fields.keys()
    assert set(FlowFileDiagnostic.model_fields) == {"file_id", "slot", "invocation_id"}
    with pytest.raises(ValidationError):
        FlowFileDiagnostic(file_id=uuid4(), slot="input", checksum_sha256="a" * 64)

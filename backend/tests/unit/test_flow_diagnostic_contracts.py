"""Observational positions never become authority or leak provider JSON."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from keystone.core.errors import ServiceError
from keystone.platform.flow.schemas.diagnostics import source_invocation
from keystone.platform.flow.workflows import diagnostics as step_diagnostics
from keystone.platform.model_access.channels.openlux import kling
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


def _manifest_and_sources(identifier):
    from keystone.platform.model_access.schemas.diagnostics import ImageSource

    output = {
        "artifact_key": f"model.{identifier}.0",
        "checksum_sha256": "a" * 64,
        "size_bytes": 10,
        "content_type": "image/png",
    }
    manifest = {"request_digest": "b" * 64, "operation": "image.generate", "outputs": [output]}
    file_id = uuid4()
    sources = [
        ImageSource(file_id=file_id, invocation_id=identifier, **{**output, "artifact_key": key})
        for key in (f"model.{identifier}.0", f"model.{identifier}.1")
    ]
    return manifest, sources


def _mutate(manifest, mutation, identifier):
    if mutation == "missing":
        return None
    if mutation == "invalid":
        return {"private": "result-secret"}
    if mutation == "operation":
        return {**manifest, "operation": "image.edit"}
    value = {
        "checksum_sha256": "c" * 64,
        "size_bytes": 11,
        "content_type": "image/jpeg",
        "artifact_key": f"model.{identifier}.1",
    }[mutation]
    return {**manifest, "outputs": [{**manifest["outputs"][0], mutation: value}]}


@pytest.mark.parametrize(
    "mutation",
    [
        None,
        "missing",
        "invalid",
        "operation",
        "checksum_sha256",
        "size_bytes",
        "content_type",
        "artifact_key",
    ],
)
async def test_source_confirmation_requires_the_exact_manifest_output(mutation):
    from unittest.mock import AsyncMock

    from keystone.platform.model_access.services.invocations import InvocationService

    identifier = uuid4()
    manifest, sources = _manifest_and_sources(identifier)
    evidence = manifest if mutation is None else _mutate(manifest, mutation, identifier)
    row = SimpleNamespace(id=identifier, operation="image.generate", result_context=evidence)
    session = SimpleNamespace(execute=AsyncMock(return_value=[row]))
    auth = SimpleNamespace(
        permissions={"model_invocation:read", "model_invocation:read_all"},
        require=lambda permission: None,
    )
    confirmed = await InvocationService(None, None).image_sources_in_transaction(
        session, auth, uuid4(), sources
    )
    # A forged index-one slot with identical bytes never borrows output zero.
    expected = {(sources[0].file_id, identifier, sources[0].artifact_key)}
    assert confirmed == (expected if mutation is None else set())


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


async def test_step_diagnostics_are_enabled_only_inside_a_recorded_step(monkeypatch):
    saved = []

    async def save(run_id, key, attempt, label, kind, state, output, error):
        saved.append((label, dict(state["input"]), output, error))

    @step_diagnostics.diagnose_step("review", project=lambda result: {"count": result})
    async def step(run_id):
        enabled = step_diagnostics.diagnostic_enabled()
        if enabled:
            step_diagnostics.diagnostic_input(size=2)
        return 2 if enabled else 0

    monkeypatch.setattr(step_diagnostics, "_save", save)
    assert not step_diagnostics.diagnostic_enabled()
    assert await step(uuid4()) == 0 and saved == []

    dbos = SimpleNamespace(workflow_id="workflow", step_id=1, step_status=None)
    monkeypatch.setattr(step_diagnostics, "DBOS", dbos)
    assert await step(uuid4()) == 2
    assert saved == [("review", {"size": 2}, {"count": 2}, None)]
    assert not step_diagnostics.diagnostic_enabled()

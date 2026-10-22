"""Declaration and Worker assembly checks without a database or an executor."""

import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ConfigDict, Field, field_serializer
from support.flow_fixture.contracts import definition

from keystone.composition import PLATFORM_CATALOG
from keystone.core.errors import ServiceError
from keystone.core.schemas import Input
from keystone.platform.flow.adapters.dbos import Engine
from keystone.platform.flow.schemas.definitions import build_definitions
from keystone.platform.flow.schemas.files import file_ids
from keystone.platform.flow.services.identity import ExecutionIdentity


def test_invalid_flow_declarations_fail_during_assembly():
    for changes, message in (
        ({"entrypoint": ""}, "entrypoint"),
        ({"entrypoint": "   "}, "entrypoint"),
        ({"timeout_seconds": 0}, "timeout"),
        ({"timeout_seconds": 86401}, "timeout"),
        ({"timeout_seconds": True}, "timeout"),
        ({"model_field": "missing"}, "model field"),
        ({"model_field": ""}, "model field"),
        ({"file_fields": ("missing",)}, "file paths"),
        ({"output_file_fields": ("missing",)}, "file paths"),
        ({"file_fields": ("source_file_id", "source_file_id")}, "file paths"),
        ({"file_fields": ("source_file_id.child",)}, "file paths"),
    ):
        with pytest.raises(ValueError, match=message):
            build_definitions((replace(definition, **changes),), PLATFORM_CATALOG.permissions)


class Attachments(Input):
    primary: UUID | None = None
    others: list[UUID]


class FileInput(Input):
    attachments: Attachments | None
    count: int
    rows: list[Attachments]
    mixed: str | int


def test_file_paths_reject_wrong_types_and_unsupported_traversal():
    for path in (
        "count",
        "mixed",
        "rows",
        "rows.primary",
        "rows.*.primary",
        "attachments..primary",
    ):
        invalid = replace(definition, input_type=FileInput, model_field=None, file_fields=(path,))
        with pytest.raises(ValueError, match="file paths"):
            build_definitions((invalid,), PLATFORM_CATALOG.permissions)


def test_nested_nullable_files_and_terminal_lists_match_extraction():
    paths = ("attachments.primary", "attachments.others")
    valid = replace(
        definition,
        input_type=FileInput,
        output_type=FileInput,
        model_field=None,
        file_fields=paths,
        output_file_fields=paths,
    )
    build_definitions((valid,), PLATFORM_CATALOG.permissions)
    first, second = uuid4(), uuid4()
    data = FileInput(
        attachments=Attachments(primary=first, others=[first, second]), count=0, rows=[], mixed="ok"
    )
    assert file_ids(data.model_dump(mode="json"), paths) == {first, second}
    data.attachments.primary = None
    assert file_ids(data.model_dump(mode="json"), paths) == {first, second}
    data.attachments = None
    assert file_ids(data.model_dump(mode="json"), paths) == set()


def test_output_schema_matches_serialization_and_field_names_everywhere():
    class Output(Input):
        model_config = ConfigDict(serialize_by_alias=True)
        amount: int = Field(alias="total")
        report_file_id: UUID = Field(alias="reportFile")

        @field_serializer("amount")
        def serialize_amount(self, value) -> str:
            return str(value)

    flow = replace(definition, output_type=Output)
    build_definitions((flow,), PLATFORM_CATALOG.permissions)
    identifier = uuid4()
    output = Output.model_validate({"total": 7, "reportFile": identifier}).model_dump(
        mode="json", by_alias=False
    )
    snapshot = flow.snapshot()
    assert snapshot["output_schema"] == flow.view()["output_schema"] == flow.output_schema()
    validator = Draft202012Validator(snapshot["output_schema"], format_checker=FormatChecker())
    validator.validate(output)
    assert not validator.is_valid({**output, "amount": 7})
    assert not validator.is_valid({"total": "7", "reportFile": str(identifier)})
    assert file_ids(output, snapshot["output_file_fields"]) == {identifier}
    with pytest.raises(ValueError, match="file paths"):
        build_definitions(
            (replace(flow, output_file_fields=("reportFile",)),), PLATFORM_CATALOG.permissions
        )


@pytest.mark.parametrize("omit_registration", [False, True])
def test_worker_imports_register_every_declared_entrypoint(omit_registration):
    # Fresh imports exercise the real public decorator. No DBOS launch, DB or private registry.
    code = (
        f"OMIT_REGISTRATION = {omit_registration!r}\n"
        + f"import sys; sys.path.insert(0, {str(Path(__file__).parents[1])!r})\n"
        + """
import sys
from support.flow_fixture import install
install()
from keystone.bootstrap import build_runtime
from keystone.composition import register_workflows
from keystone.core.config import Settings

runtime = build_runtime(Settings(config_file=None, database_url=None, flow_enabled=False))
assert not any(name.startswith('keystone.businesses.') and name.endswith('.workflow')
               for name in sys.modules)
from dbos import DBOS
original = DBOS.workflow
registered = set()
def observe(**options):
    decorate = original(**options)
    def apply(function):
        wrapped = decorate(function)
        registered.add(options.get('name'))
        return wrapped
    return apply
DBOS.workflow = staticmethod(observe)
if not OMIT_REGISTRATION:
    register_workflows()
expected = {item.entrypoint for item in runtime.flow_definitions.values()}
assert expected <= registered, f'Missing Worker entrypoints: {expected - registered}'
"""
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=45
    )
    if omit_registration:
        assert result.returncode != 0 and "Missing Worker entrypoints" in result.stderr
    else:
        assert result.returncode == 0, result.stderr


def engine_with_error(error):
    engine = Engine.__new__(Engine)
    engine.client = SimpleNamespace(
        list_workflows_async=AsyncMock(
            return_value=[
                SimpleNamespace(
                    status="ERROR",
                    output=None,
                    error=RuntimeError(error),
                    app_version="test",
                    name="document_review",
                )
            ]
        )
    )
    return engine


async def test_flow_preserves_safe_execution_errors():
    for code in (
        "model_not_found",
        "model_target_changed",
        "model_input_too_large",
        "model_response_too_large",
        "model_stream_incomplete",
        "model_interrupted",
        "model_invalid_request",
        "upstream_model_not_found",
        "model_rate_limited",
        "execution_not_authorized",
        "flow_invalid_output",
        "flow_budget_exceeded",
        "flow_timeout",
    ):
        assert (await engine_with_error(code).state(uuid4()))["error_code"] == code


async def test_flow_never_projects_unknown_codes_or_exception_messages():
    for error in ("unknown_code", "model_invalid_request: secret body"):
        assert (await engine_with_error(error).state(uuid4()))[
            "error_code"
        ] == "flow_execution_failed"


async def test_execution_version_is_queried_again_at_each_authorization(monkeypatch):
    current = "original"
    run = SimpleNamespace(
        definition_key=definition.key,
        execution_version=current,
        status="pending",
        cancel_requested_at=None,
        actor_id=uuid4(),
        personal_access_token_id=None,
    )
    runtime = SimpleNamespace(
        database=None,
        settings=None,
        flow_definitions={definition.key: definition},
        execution_version=lambda: current,
    )
    identity = ExecutionIdentity(runtime, uuid4())
    session = SimpleNamespace(scalar=AsyncMock(return_value=run))
    authorize = AsyncMock(return_value=SimpleNamespace(require=lambda _: None))
    monkeypatch.setattr("keystone.platform.flow.services.identity.load_authorization", authorize)
    await identity.authorize(session)
    current = "changed"
    with pytest.raises(ServiceError) as error:
        await identity.authorize(session)
    assert error.value.code == "execution_version_unavailable"
    assert authorize.await_count == 1


async def test_flow_directory_uses_its_application_version_source():
    from contextlib import asynccontextmanager
    from dataclasses import replace

    from keystone.bootstrap import build_runtime
    from keystone.core.config import Settings

    runtime = build_runtime(Settings(config_file=None, database_url=None, flow_enabled=False))
    other = replace(runtime, execution_version=lambda: "another-application")

    @asynccontextmanager
    async def access(*args):
        yield None, SimpleNamespace(permissions=set())

    identity = SimpleNamespace(settings=runtime.settings, access=access)
    first = await runtime.flow_service(identity).definitions()
    second = await other.flow_service(identity).definitions()
    assert first["execution_version"] == runtime.execution_version()
    assert second["execution_version"] == "another-application"

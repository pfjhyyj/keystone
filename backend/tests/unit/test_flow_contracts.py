"""Declaration and Worker assembly checks without a database or an executor."""

import subprocess
import sys
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ConfigDict, Field, field_serializer
from support.flow_fixture.contracts import definition
from support.flow_packages import snapshot_for
from support.multi_role_flow import PACKAGE

from keystone.composition import PLATFORM_CATALOG
from keystone.core.errors import ServiceError
from keystone.core.schemas import Input
from keystone.platform.flow.adapters.dbos import Engine
from keystone.platform.flow.schemas.definitions import ModelRole, build_definitions
from keystone.platform.flow.schemas.files import compile_file_paths, extract_file_ids
from keystone.platform.flow.schemas.packages import build_packages
from keystone.platform.flow.schemas.snapshots import public_snapshot, read_snapshot
from keystone.platform.flow.services.identity import ExecutionIdentity


def test_invalid_flow_declarations_fail_during_assembly():
    for changes, message in (
        ({"entrypoint": ""}, "entrypoint"),
        ({"entrypoint": "   "}, "entrypoint"),
        ({"timeout_seconds": 0}, "timeout"),
        ({"timeout_seconds": 86401}, "timeout"),
        ({"timeout_seconds": True}, "timeout"),
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
        "attachments..primary",
    ):
        with pytest.raises(ValueError, match="file paths"):
            invalid = replace(
                definition, input_model=FileInput, model_roles=(), file_fields=(path,)
            )
            build_definitions((invalid,), PLATFORM_CATALOG.permissions)


def test_nested_nullable_files_and_terminal_lists_match_extraction():
    paths = ("attachments.primary", "attachments.others")
    valid = replace(
        definition,
        input_model=FileInput,
        output_model=FileInput,
        model_roles=(),
        file_fields=paths,
        output_file_fields=paths,
    )
    build_definitions((valid,), PLATFORM_CATALOG.permissions)
    first, second = uuid4(), uuid4()
    data = FileInput(
        attachments=Attachments(primary=first, others=[first, second]), count=0, rows=[], mixed="ok"
    )
    assert extract_file_ids(
        data.model_dump(mode="json"), compile_file_paths(valid.input_schema(), paths)
    ) == {first, second}
    data.attachments.primary = None
    assert extract_file_ids(
        data.model_dump(mode="json"), compile_file_paths(valid.input_schema(), paths)
    ) == {first, second}
    data.attachments = None
    assert (
        extract_file_ids(
            data.model_dump(mode="json"), compile_file_paths(valid.input_schema(), paths)
        )
        == set()
    )


def test_output_schema_matches_serialization_and_field_names_everywhere():
    class Output(Input):
        model_config = ConfigDict(serialize_by_alias=True)
        amount: int = Field(alias="total")
        report_file_id: UUID = Field(alias="reportFile")

        @field_serializer("amount")
        def serialize_amount(self, value) -> str:
            return str(value)

    flow = replace(definition, output_model=Output)
    build_definitions((flow,), PLATFORM_CATALOG.permissions)
    identifier = uuid4()
    output = Output.model_validate({"total": 7, "reportFile": identifier}).model_dump(
        mode="json", by_alias=False
    )
    snapshot = snapshot_for(flow)
    assert snapshot["output_schema"] == flow.view()["output_schema"] == flow.output_schema()
    validator = Draft202012Validator(snapshot["output_schema"], format_checker=FormatChecker())
    validator.validate(output)
    assert not validator.is_valid({**output, "amount": 7})
    assert not validator.is_valid({"total": "7", "reportFile": str(identifier)})
    assert extract_file_ids(
        output, compile_file_paths(flow.output_schema(), snapshot["output_file_fields"])
    ) == {identifier}
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

runtime = build_runtime(Settings(config_file=None, database_url=None))
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
        "model_authentication_failed",
        "model_insufficient_credits",
        "model_access_denied",
        "model_invalid_request",
        "upstream_model_not_found",
        "model_rate_limited",
        "model_invalid_image",
        "model_result_source_denied",
        "model_task_timeout",
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
        definition_snapshot=snapshot_for(definition),
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

    runtime = build_runtime(Settings(config_file=None, database_url=None))
    other = replace(runtime, execution_version=lambda: "another-application")

    @asynccontextmanager
    async def access(*args):
        yield None, SimpleNamespace(permissions=set())

    identity = SimpleNamespace(settings=runtime.settings, access=access)
    first = await runtime.flow_service(identity).definitions()
    second = await other.flow_service(identity).definitions()
    assert first["execution_version"] == runtime.execution_version()
    assert second["execution_version"] == "another-application"


def test_explicit_package_and_roles_are_validated_and_frozen():
    definitions, owners = build_packages((PACKAGE,), PLATFORM_CATALOG.permissions)
    assert owners[PACKAGE.definitions[0].key] == PACKAGE.key
    with pytest.raises(TypeError):
        definitions["other"] = definition
    with pytest.raises(FrozenInstanceError):
        PACKAGE.key = "other"
    for operation in ("video.generate", "unknown"):
        with pytest.raises(ValueError):
            ModelRole("review", "Review", operation)
    with pytest.raises(ValueError, match="Duplicate"):
        replace(definition, model_roles=(ModelRole("review", "A"), ModelRole("review", "B")))
    with pytest.raises(ValueError):
        build_packages((PACKAGE, PACKAGE), PLATFORM_CATALOG.permissions)


def test_array_file_paths_aliases_nulls_overlap_and_complexity():
    schema = {
        "type": "object",
        "properties": {
            "rows": {
                "type": "array",
                "items": {
                    "anyOf": [
                        {"type": "null"},
                        {
                            "type": "object",
                            "properties": {
                                "file": {"anyOf": [{"type": "null"}, {"type": "string"}]}
                            },
                        },
                    ]
                },
            },
            "ids": {"type": "array", "items": {"type": "string"}},
        },
    }
    identifier = str(uuid4())
    plans = compile_file_paths(schema, ("rows.*.file", "ids"))
    assert extract_file_ids(
        {"rows": [None, {"file": None}, {"file": identifier}], "ids": [identifier]}, plans
    ) == {UUID(identifier)}
    with pytest.raises(ValueError, match="Overlapping"):
        compile_file_paths(schema, ("ids", "ids.*"))
    with pytest.raises(ValueError):
        compile_file_paths(schema, ("rows.0.file",))
    with pytest.raises(ValueError, match="traversal"):
        extract_file_ids({"rows": [None] * 4097, "ids": []}, plans)
    for value in ({"rows": [{"file": "bad"}], "ids": []}, {"rows": [{}], "ids": []}):
        with pytest.raises(ValueError):
            extract_file_ids(value, plans)


def test_snapshot_projection_keeps_bindings_private_and_formats_fail_closed():
    snapshot = snapshot_for(definition)
    projected = public_snapshot(snapshot)
    assert "model_bindings" not in projected
    assert "api_base" not in str(projected)
    assert read_snapshot(snapshot) == snapshot
    assert read_snapshot({"output_schema": {}, "output_file_fields": []}) == {
        "output_schema": {},
        "output_file_fields": [],
    }
    for invalid in ({**snapshot, "format_version": 99}, {**snapshot, "model_bindings": {}}):
        with pytest.raises(ServiceError) as error:
            read_snapshot(invalid)
        assert error.value.code == "flow_snapshot_unavailable"


async def test_text_context_revalidates_models_before_any_model_io(monkeypatch):
    from unittest.mock import AsyncMock

    from pydantic import ValidationError

    from keystone.platform.flow.workflows.context import flow_context
    from keystone.platform.model_access.schemas.invocations import Invoke, TextRequest

    ctx = flow_context(uuid4())
    model = AsyncMock()
    monkeypatch.setattr(type(ctx), "_model", model)
    messages = [{"role": "user", "content": "hello"}]
    for request in (
        Invoke(model_key="chat", messages=messages, stream=True),
        TextRequest(messages=messages).model_copy(update={"max_tokens": 9999}),
        {"messages": messages, "stream": True},
    ):
        with pytest.raises(ValidationError):
            await ctx.invoke_text("review", request, node_key="review")
    model.assert_not_awaited()


def test_contracts_must_serialize_as_root_objects():
    from pydantic import model_serializer

    from keystone.core.schemas import Input

    class StringContract(Input):
        value: str

        @model_serializer
        def serialize(self) -> str:
            return self.value

    for field in ("input_model", "output_model"):
        with pytest.raises(ValueError, match="object models"):
            replace(definition, **{field: StringContract})


def test_file_schema_references_fail_closed_without_external_resolution():
    for ref in ("https://example.invalid/schema", "#/$defs/recursive", 123):
        schema = {
            "type": "object",
            "properties": {"file": {"$ref": ref}},
            "$defs": {"recursive": {"$ref": "#/$defs/recursive"}},
        }
        with pytest.raises(ValueError):
            compile_file_paths(schema, ("file",))
    snapshot = snapshot_for(definition)
    snapshot["output_schema"] = {"type": "object", "properties": {"report_file_id": {"$ref": 123}}}
    with pytest.raises(ServiceError) as error:
        read_snapshot(snapshot)
    assert error.value.code == "flow_snapshot_unavailable"

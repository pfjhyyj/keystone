"""Declaration and Worker assembly checks without a database or an executor."""

import subprocess
import sys
from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ConfigDict, Field, field_serializer

from keystone.businesses.document_review.contracts import definition
from keystone.core.schemas import Input
from keystone.platform.flow.catalog import build_definitions, file_ids
from keystone.platform.identity.policy import PERMISSIONS


@pytest.mark.parametrize(
    "changes,message",
    [
        ({"entrypoint": ""}, "entrypoint"),
        ({"entrypoint": "   "}, "entrypoint"),
        ({"model_field": "missing"}, "model field"),
        ({"model_field": ""}, "model field"),
        ({"file_fields": ("missing",)}, "file paths"),
        ({"output_file_fields": ("missing",)}, "file paths"),
        ({"file_fields": ("source_file_id", "source_file_id")}, "file paths"),
        ({"file_fields": ("source_file_id.child",)}, "file paths"),
    ],
)
def test_invalid_flow_declarations_fail_during_assembly(changes, message):
    with pytest.raises(ValueError, match=message):
        build_definitions((replace(definition, **changes),), PERMISSIONS)


def test_distinct_definitions_cannot_share_an_entrypoint():
    with pytest.raises(ValueError, match="duplicate Flow entrypoint"):
        build_definitions((definition, replace(definition, key="second")), PERMISSIONS)


def test_platform_starts_with_an_empty_flow_directory(monkeypatch):
    from fastapi.testclient import TestClient

    from keystone import composition
    from keystone.bootstrap import create_app
    from keystone.core.asyncio import new_event_loop
    from keystone.core.config import Settings

    monkeypatch.setattr(composition, "FLOW_DEFINITIONS", ())
    with TestClient(
        create_app(Settings(config_file=None, database_url=None)),
        backend_options={"loop_factory": new_event_loop},
    ) as client:
        assert client.get("/api/v1/health").status_code == 200
        assert not client.app.state.runtime.flow_definitions


class Attachments(Input):
    primary: UUID | None = None
    others: list[UUID]


class FileInput(Input):
    attachments: Attachments | None
    count: int
    rows: list[Attachments]
    mixed: str | int


@pytest.mark.parametrize(
    "path", ["count", "mixed", "rows", "rows.primary", "rows.*.primary", "attachments..primary"]
)
def test_file_paths_reject_wrong_types_and_unsupported_traversal(path):
    invalid = replace(definition, input_type=FileInput, model_field=None, file_fields=(path,))
    with pytest.raises(ValueError, match="file paths"):
        build_definitions((invalid,), PERMISSIONS)


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
    build_definitions((valid,), PERMISSIONS)
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
    build_definitions((flow,), PERMISSIONS)
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
        build_definitions((replace(flow, output_file_fields=("reportFile",)),), PERMISSIONS)


@pytest.mark.parametrize("omit_registration", [False, True])
def test_worker_imports_register_every_declared_entrypoint(omit_registration):
    # Fresh imports exercise the real public decorator. No DBOS launch, DB or private registry.
    code = (
        f"OMIT_REGISTRATION = {omit_registration!r}\n"
        + """
import sys
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

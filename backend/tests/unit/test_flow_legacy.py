"""Persisted contracts retain their original limits and execution identity."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from support.flow_fixture.contracts import definition
from support.flow_packages import snapshot_for

from keystone.core.errors import ServiceError
from keystone.platform.flow.schemas.files import extract_legacy_file_ids
from keystone.platform.flow.schemas.snapshots import read_snapshot
from keystone.platform.flow.services.reconciliation import matches_observation


def legacy(schema, paths):
    return {"output_schema": schema, "output_file_fields": paths}


def test_legacy_union_paths_and_limits_remain_readable():
    union = {"anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]}
    cases = [
        legacy({"properties": {"files": union}}, ["files"]),
        legacy(
            {"properties": {f"f{i}": {"type": "string"} for i in range(33)}},
            [f"f{i}" for i in range(33)],
        ),
        legacy({"properties": {"f文件": {"type": "string"}}}, ["f文件"]),
        legacy({"description": "x" * 65536}, []),
    ]
    schema = {"type": "string"}
    for _ in range(9):
        schema = {"properties": {"nested": schema}}
    cases.append(legacy(schema, [".".join(["nested"] * 9)]))
    for snapshot in cases:
        assert read_snapshot(snapshot) == snapshot
        modern = snapshot_for(definition)
        modern.update(snapshot)
        with pytest.raises(ServiceError) as error:
            read_snapshot(modern)
        assert error.value.code == "flow_snapshot_unavailable"
    identifier = uuid4()
    for value in (str(identifier), [str(identifier), None], [str(identifier)] * 4097):
        assert extract_legacy_file_ids({"files": value}, ["files"]) == {identifier}
    with pytest.raises(ValueError, match="Too many files"):
        extract_legacy_file_ids({"files": [str(uuid4()) for _ in range(101)]}, ["files"])


def test_legacy_malformed_references_fail_closed():
    for ref in (123, "https://example.invalid/schema", "#/$defs/cycle"):
        snapshot = legacy(
            {"properties": {"file": {"$ref": ref}}, "$defs": {"cycle": {"$ref": "#/$defs/cycle"}}},
            ["file"],
        )
        with pytest.raises(ServiceError) as error:
            read_snapshot(snapshot)
        assert error.value.code == "flow_snapshot_unavailable"


@pytest.mark.parametrize("modern", [False, True])
@pytest.mark.parametrize("same_artifact", [False, True])
@pytest.mark.parametrize("definition_present", [False, True])
def test_observation_uses_persisted_identity(modern, same_artifact, definition_present):
    snapshot = snapshot_for(definition) if modern else legacy({}, [])
    if modern:
        snapshot["entrypoint"] = "job.original"
    runtime = SimpleNamespace(
        flow_definitions={"job": SimpleNamespace(entrypoint="job.renamed")}
        if definition_present
        else {},
        execution_version=lambda: "current",
    )
    run = SimpleNamespace(
        definition_key="job",
        execution_version="current" if same_artifact else "old",
        definition_snapshot=snapshot,
    )
    state = {"version": run.execution_version, "name": "job.original"}
    expected = modern or not (same_artifact and definition_present)
    assert matches_observation(runtime, run, state) == expected
    assert not matches_observation(runtime, run, {**state, "version": "wrong"})
    if modern:
        assert not matches_observation(runtime, run, {**state, "name": "job.renamed"})

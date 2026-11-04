"""Persisted contracts require the current schema and fixed execution identity."""

from types import SimpleNamespace

import pytest
from support.flow_fixture.contracts import definition
from support.flow_packages import snapshot_for

from keystone.core.errors import ServiceError
from keystone.platform.flow.schemas.snapshots import read_snapshot
from keystone.platform.flow.services.reconciliation import matches_observation


def test_observation_uses_persisted_identity():
    snapshot = snapshot_for(definition)
    snapshot["entrypoint"] = "job.original"
    run = SimpleNamespace(
        definition_key="job",
        execution_version="old",
        definition_snapshot=snapshot,
    )
    state = {"version": run.execution_version, "name": "job.original"}
    assert matches_observation(run, state)
    assert not matches_observation(run, {**state, "version": "wrong"})
    assert not matches_observation(run, {**state, "name": "job.renamed"})


@pytest.mark.parametrize("field", ["format_version", "entrypoint"])
def test_snapshot_requires_current_contract(field):
    snapshot = snapshot_for(definition)
    snapshot.pop(field)
    with pytest.raises(ServiceError) as error:
        read_snapshot(snapshot)
    assert error.value.code == "flow_snapshot_unavailable"

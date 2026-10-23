"""Artifact locations are validated without database or storage I/O."""

from uuid import uuid4

import pytest

from keystone.platform.flow.services.artifacts import artifact_location


@pytest.mark.parametrize("key", ["", "x" * 87, "line\nbreak", None])
def test_artifact_key_validation_precedes_io(key):
    with pytest.raises(ValueError):
        artifact_location(uuid4(), key)


def test_artifact_location_preserves_opaque_keys_and_canonicalizes_run_ids():
    run_id = uuid4()
    assert artifact_location(str(run_id).upper(), "preview.v2.proxy") == (
        f"flow:{run_id}",
        "artifact.preview.v2.proxy",
    )

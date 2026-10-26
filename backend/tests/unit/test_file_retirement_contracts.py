"""Pure validation for the trusted maintenance manifest."""

from uuid import uuid4

import pytest

from keystone.core.errors import ServiceError
from keystone.platform.files.services.references import FileReferenceService


def test_retirement_manifest_normalizes_exact_claims_and_ids():
    identifier = uuid4()
    claims, ids = FileReferenceService.validate_retirement_scope(
        [("example:1", "output", str(identifier)), ("example:1", "output", identifier)],
        [str(identifier), identifier],
    )
    assert claims == {("example:1", "output", identifier)} and ids == {identifier}


@pytest.mark.parametrize(
    "owner,slot",
    [("", "output"), ("x" * 161, "output"), ("example:1", ""), ("example:1", "x" * 101)],
)
def test_retirement_rejects_invalid_reference_keys(owner, slot):
    with pytest.raises(ValueError, match="reference key"):
        FileReferenceService.validate_retirement_scope([(owner, slot, uuid4())], [])


@pytest.mark.parametrize("dimension", ["claims", "artifacts", "affected"])
def test_retirement_limits_apply_before_native_leases(dimension):
    claims, artifacts = [], []
    if dimension == "claims":
        identifier = uuid4()
        claims = [(f"example:{i}", "input", identifier) for i in range(5001)]
    elif dimension == "artifacts":
        artifacts = [uuid4() for _ in range(501)]
    else:
        artifacts = [uuid4() for _ in range(500)]
        claims = [("example:1", "input", uuid4()) for _ in range(501)]
    with pytest.raises(ServiceError) as rejected:
        FileReferenceService.validate_retirement_scope(claims, artifacts)
    assert rejected.value.code == "file_reference_limit"

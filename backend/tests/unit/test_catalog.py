"""Module declarations compose without overriding another module's authority."""

import pytest

from keystone.core.catalog import Catalog, combine_catalogs


@pytest.mark.parametrize(
    "catalog",
    [
        Catalog(permissions={"records:read": "Read records"}),
        Catalog(audit_actions={"records.save": {"after"}}),
    ],
)
def test_platform_catalog_rejects_duplicate_declarations_even_when_equal(catalog):
    with pytest.raises(ValueError, match="Duplicate platform"):
        combine_catalogs((catalog, catalog))


def test_platform_catalog_preserves_permissions_and_freezes_audit_masks():
    fields = {"before", "after"}
    combined = combine_catalogs(
        (
            Catalog(permissions={"records:read": "Read records"}),
            Catalog(audit_actions={"records.save": fields}),
        )
    )
    fields.add("secret")
    assert dict(combined.permissions) == {"records:read": "Read records"}
    assert combined.audit_actions["records.save"] == {"before", "after"}
    with pytest.raises(TypeError):
        combined.permissions["records:write"] = "Write records"

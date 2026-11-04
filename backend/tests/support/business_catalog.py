"""Lightweight declarations for test business modules, without HTTP or SDK imports."""

from keystone import composition
from keystone.core.catalog import Catalog

PREFIX = "test_files.asset:"
DECLARATION = Catalog(
    permissions={PREFIX + key: key for key in ("create", "inspect", "content_read", "manage")},
    audit_actions={"test_files.asset.save": {"status"}},
)


def install_catalog(monkeypatch=None):
    if any(name == "test_files" for name, _ in composition.BUSINESS_CATALOGS):
        return
    catalogs = (*composition.BUSINESS_CATALOGS, ("test_files", DECLARATION))
    if monkeypatch is None:
        composition.BUSINESS_CATALOGS = catalogs
    else:
        monkeypatch.setattr(composition, "BUSINESS_CATALOGS", catalogs)

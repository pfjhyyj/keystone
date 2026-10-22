"""Lightweight permission and audit declarations owned by this module."""

from keystone.core.catalog import Catalog

CATALOG = Catalog(
    permissions={},
    audit_actions={"worker.retire": set(), "worker.reactivate": set()},
)

"""Lightweight permission and audit declarations owned by this module."""

from keystone.core.catalog import Catalog

CATALOG = Catalog(
    audit_actions={
        "file.upload.start": set(),
        "file.upload.complete": {"size_bytes"},
        "file.upload.failure": {"reason"},
        "file.upload.residue_cleanup": set(),
        "file.update": {"before", "status", "after", "name"},
        "file.delete.request": set(),
        "file.delete.complete": set(),
        "file.cleanup.failure": {"reason"},
        "file.reference": {"added", "owner", "slot"},
    },
)

"""Lightweight permission and audit declarations owned by this module."""

from keystone.core.catalog import Catalog

CATALOG = Catalog(
    permissions={"audit:read": "查询审计日志"},
    audit_actions={},
)

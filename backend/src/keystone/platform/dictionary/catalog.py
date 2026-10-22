"""Lightweight permission and audit declarations owned by this module."""

from keystone.core.catalog import Catalog

CATALOG = Catalog(
    permissions={"dictionary:read": "查询字典与历史选项", "dictionary:manage": "管理字典与选项"},
    audit_actions={
        "dictionary.definition.create": {"before", "after", "name", "key", "description"},
        "dictionary.definition.update": {"before", "is_active", "after", "name", "description"},
        "dictionary.item.create": {"before", "label", "value", "after", "sort_order"},
        "dictionary.item.update": {"before", "label", "is_active", "after", "sort_order"},
    },
)

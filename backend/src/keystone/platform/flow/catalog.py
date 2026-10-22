"""Lightweight permission and audit declarations owned by this module."""

from keystone.core.catalog import Catalog

CATALOG = Catalog(
    permissions={
        "flow:start": "发起流程",
        "flow:read": "读取本人流程",
        "flow:cancel": "取消流程",
        "flow:manage": "扩大流程管理范围",
    },
    audit_actions={
        "flow.start": {"definition_key"},
        "flow.cancel": set(),
        "flow.complete": {"error_code", "status"},
    },
)

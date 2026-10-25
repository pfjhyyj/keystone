"""Lightweight permission and audit declarations owned by this module."""

from keystone.core.catalog import Catalog

CATALOG = Catalog(
    permissions={
        "model:invoke": "调用模型与查询可用目录",
        "model:manage": "管理模型配置与渠道余额",
        "model_invocation:read": "查询本人模型调用与费用",
        "model_invocation:read_all": "扩大模型调用查询至全部用户",
    },
    audit_actions={
        "model.channel.delete": {"name", "provider"},
        "model.entry.delete": {"key", "name"},
        "model.cost.recheck": {"previous_status"},
        "model.cost.bind": {"source"},
        "model.channel.create": {
            "after",
            "api_base",
            "api_key_changed",
            "before",
            "billing_api_key_changed",
            "has_api_key",
            "has_billing_api_key",
            "is_active",
            "name",
            "provider",
            "upstream_account_ref",
        },
        "model.channel.update": {
            "after",
            "api_base",
            "api_key_changed",
            "before",
            "billing_api_key_changed",
            "has_api_key",
            "has_billing_api_key",
            "is_active",
            "name",
            "upstream_account_ref",
        },
        "model.entry.create": {
            "name",
            "after",
            "before",
            "channel_id",
            "is_active",
            "key",
            "provider_model",
            "operations",
        },
        "model.entry.update": {
            "name",
            "before",
            "channel_id",
            "is_active",
            "after",
            "provider_model",
            "operations",
        },
        "model.balance.refresh": {"status"},
        "model.invocation.recover": {"source"},
    },
)

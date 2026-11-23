"""Lightweight permission and audit declarations owned by this module."""

from keystone.core.catalog import Catalog

CATALOG = Catalog(
    permissions={
        "messaging:manage": "管理消息渠道与接收方",
        "message_delivery:read": "查看消息投递记录（不含内容）",
    },
    audit_actions={
        "messaging.channel.create": {
            "name",
            "provider",
            "app_id",
            "is_active",
            "credential_changed",
            "destination_key",
        },
        "messaging.channel.update": {
            "name",
            "provider",
            "app_id",
            "is_active",
            "credential_changed",
            "destination_key",
        },
        "messaging.channel.delete": {
            "name",
            "provider",
            "destination_keys",
            "deliveries_removed",
        },
        "messaging.destination.create": {"key", "channel_id", "name", "is_active"},
        "messaging.destination.update": {"key", "channel_id", "name", "is_active"},
        "messaging.destination.delete": {"key", "channel_id", "name", "deliveries_removed"},
        "messaging.destination.test": {"delivery_id"},
    },
)

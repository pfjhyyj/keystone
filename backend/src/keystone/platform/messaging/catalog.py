"""Lightweight permission and audit declarations owned by this module."""

from keystone.core.catalog import Catalog

CATALOG = Catalog(
    permissions={
        "messaging:manage": "管理消息渠道与接收方",
        "message_delivery:read": "查看消息投递记录（不含内容）",
    },
    audit_actions={
        "messaging.channel.create": {"name", "provider", "is_active", "credential_changed"},
        "messaging.channel.update": {"name", "provider", "is_active", "credential_changed"},
        "messaging.destination.create": {"key", "channel_id", "name", "is_active"},
        "messaging.destination.update": {"key", "channel_id", "name", "is_active"},
        "messaging.destination.test": {"delivery_id"},
    },
)

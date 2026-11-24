"""Lightweight permission and audit declarations owned by this module."""

from keystone.core.catalog import Catalog

CATALOG = Catalog(
    permissions={
        "messaging:manage": "管理消息渠道、发送目标与飞书账号绑定",
        "messaging:delegate": "设置群的执行用户，让群成员以该用户身份操作",
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
        "messaging.destination.delegate": {
            "key",
            "channel_id",
            "executing_user_id",
            "previous_user_id",
        },
        "messaging.destination.test": {"delivery_id"},
        "messaging.account.create": {"channel_id", "user_id", "external_user_id"},
        "messaging.account.delete": {"channel_id", "user_id", "external_user_id"},
        "messaging.account.test": {"delivery_id"},
        "messaging.account.self_bind": {"channel_id", "external_user_id", "replaced", "via"},
        "messaging.account.self_unbind": {"channel_id", "external_user_id"},
        "messaging.inbound.card_action": set(),
        "messaging.inbound.command": set(),
    },
)

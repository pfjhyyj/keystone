"""Lightweight permission and audit declarations owned by this module."""

from keystone.core.catalog import Catalog

CATALOG = Catalog(
    permissions={
        "user:read": "查看用户",
        "user:create": "创建用户",
        "user:update": "修改与启停用户",
        "user:reset_password": "重置用户密码",
        "user:credentials": "撤销用户凭据",
        "role:read": "查看角色和权限目录",
        "role:create": "创建角色",
        "role:update": "修改角色权限",
        "role:delete": "删除角色",
        "role:assign": "分配或移除角色",
    },
    audit_actions={
        "identity.login": {"reason"},
        "identity.logout": {"user_id"},
        "identity.password.change": {"sessions_revoked"},
        "identity.user.create": {"display_name", "username"},
        "identity.user.update": {"before", "display_name", "after"},
        "identity.user.enable": {"is_active"},
        "identity.user.disable": {"is_active", "sessions_revoked", "tokens_revoked"},
        "identity.password.reset": {"sessions_revoked"},
        "identity.credentials.revoke": {"sessions_revoked", "user_id", "tokens_revoked"},
        "identity.role.create": {"key", "name", "permissions"},
        "identity.role.update": {"before", "after", "permissions", "name", "description"},
        "identity.role.delete": {"key", "permissions"},
        "identity.role.assign": {"assigned", "role_id"},
        "identity.pat.issue": {"user_id", "permissions"},
        "identity.pat.revoke": {"user_id"},
        "identity.admin.initialize": {"source", "username"},
    },
)

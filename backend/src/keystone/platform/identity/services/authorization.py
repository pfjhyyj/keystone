"""Current DB grants + ephemeral Casbin enforcer, adapted from Ether AuthorizationService."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select, text

from keystone.core.errors import ServiceError
from keystone.core.time import utcnow
from keystone.platform.identity.adapters.rbac import Enforcer, enforcer_for_roles
from keystone.platform.identity.adapters.security import digest
from keystone.platform.identity.models import (
    LoginSession,
    PersonalAccessToken,
    Role,
    RolePermission,
    User,
    UserRole,
)

ADMIN_KEY = "admin"


async def identity_lock(session, *, write=False):
    # Shared reads and exclusive writes coordinate across processes. No external I/O under lock.
    fn = "pg_advisory_xact_lock" if write else "pg_advisory_xact_lock_shared"
    await session.execute(text(f"SELECT {fn}(73190421)"))


def permission_catalog(session):
    return session.info["catalog"].permissions


@dataclass
class Authorization:
    user: User
    credential: LoginSession | PersonalAccessToken | None
    kind: str
    administrator: bool
    enforcer: Enforcer
    permissions: set[str]
    declared_permissions: frozenset[str]

    def require(self, permission):
        if permission not in self.permissions or not self.enforcer.enforce(
            str(self.user.id), permission
        ):
            raise ServiceError(403, "permission_denied", "没有执行此操作的权限")

    def grantable(self, permissions):
        if set(permissions) - self.declared_permissions:
            raise ServiceError(422, "unknown_permission", "存在未声明的权限")
        for permission in permissions:
            self.require(permission)


async def role_permissions(session, role):
    catalog = permission_catalog(session)
    if role.key == ADMIN_KEY:
        return set(catalog)
    return (
        set(
            await session.scalars(
                select(RolePermission.permission_key).where(RolePermission.role_id == role.id)
            )
        )
        & catalog.keys()
    )


async def load_authorization(session, user_id: UUID, *, pat_id: UUID | None = None):
    """Trusted user/Flow context only. Call inside a short transaction with identity_lock."""
    user = await session.get(User, user_id, populate_existing=True)
    if user is None or not user.is_active:
        raise ServiceError(401, "authentication_required", "请重新登录")
    pat = await session.get(PersonalAccessToken, pat_id, populate_existing=True) if pat_id else None
    if pat_id and (
        pat is None or pat.user_id != user_id or pat.revoked_at or pat.expires_at <= utcnow()
    ):
        raise ServiceError(401, "authentication_required", "凭据已失效")
    roles, administrator = await role_grants(session, user_id)
    enforcer, grants = enforcer_for_roles(user_id, roles)
    if pat is not None:
        grants &= set(pat.permissions)
    if user.password_change_required:
        grants.clear()
    return Authorization(
        user,
        pat,
        "pat" if pat else "trusted",
        administrator,
        enforcer,
        grants,
        frozenset(permission_catalog(session)),
    )


async def authenticate(session, credential):
    if credential is None or not 20 <= len(credential.secret) <= 256:
        raise ServiceError(401, "authentication_required", "请先登录")
    model = LoginSession if credential.kind == "session" else PersonalAccessToken
    token = await session.scalar(
        select(model).where(model.token_digest == digest(credential.secret))
    )
    if token is None or token.revoked_at or token.expires_at <= utcnow():
        raise ServiceError(401, "authentication_required", "凭据已失效")
    auth = await load_authorization(
        session, token.user_id, pat_id=token.id if credential.kind == "pat" else None
    )
    auth.credential, auth.kind = token, credential.kind
    return auth


async def manageable(session, auth, target):
    current = await load_authorization_for_target(session, target)
    if current[1] and not auth.administrator:
        raise ServiceError(403, "administrator_required", "只有内置管理员可管理管理员账户")
    auth.grantable(current[0])


async def load_authorization_for_target(session, target):
    # Disabled/forced-change targets still retain their role scope for management checks.
    roles, administrator = await role_grants(session, target.id)
    return set().union(*roles.values()), administrator


async def role_grants(session, user_id):
    """Load current role grants in one query; credential/account restrictions belong to callers."""
    catalog = permission_catalog(session)
    rows = await session.execute(
        select(Role.id, Role.key, RolePermission.permission_key)
        .join(UserRole, UserRole.role_id == Role.id)
        .outerjoin(RolePermission, RolePermission.role_id == Role.id)
        .where(UserRole.user_id == user_id)
    )
    roles, administrator = {}, False
    for role_id, key, permission in rows:
        grants = roles.setdefault(role_id, set())
        if key == ADMIN_KEY:
            administrator = True
            grants.update(catalog)
        elif permission in catalog:
            grants.add(permission)
    return roles, administrator


async def protect_last_admin(session, user):
    _, admin = await load_authorization_for_target(session, user)
    if not admin or not user.is_active:
        return
    other = await session.scalar(
        select(User.id)
        .join(UserRole)
        .join(Role)
        .where(Role.key == ADMIN_KEY, User.is_active.is_(True), User.id != user.id)
        .limit(1)
    )
    if other is None:
        raise ServiceError(409, "last_administrator", "必须保留至少一位启用的管理员")

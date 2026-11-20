"""Current role grants, bounded by the original credential's permission scope."""

import asyncio
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select, text

from keystone.core.errors import ServiceError
from keystone.core.time import utcnow
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
IDENTITY_LOCK_TIMEOUT_SECONDS = 3


async def identity_lock(session, *, identity_write=False):
    # Shared reads and exclusive writes coordinate across processes. No external I/O under lock.
    fn = "pg_try_advisory_xact_lock" if identity_write else "pg_try_advisory_xact_lock_shared"
    try:
        async with asyncio.timeout(IDENTITY_LOCK_TIMEOUT_SECONDS):
            # Do not leave a blocking server query behind when a caller times out or cancels.
            while not await session.scalar(text(f"SELECT {fn}(73190421)")):
                await asyncio.sleep(0.05)
    except TimeoutError:
        raise ServiceError(503, "identity_busy", "身份操作繁忙，请稍后重试") from None


def permission_catalog(session):
    return session.info["catalog"].permissions


@dataclass
class Authorization:
    user: User
    credential: LoginSession | PersonalAccessToken | None
    kind: str
    permissions: set[str]
    declared_permissions: frozenset[str]
    role_ids: frozenset[UUID]

    def require(self, permission):
        if permission not in self.permissions:
            raise ServiceError(403, "permission_denied", "没有执行此操作的权限")

    def grantable(self, permissions):
        if set(permissions) - self.declared_permissions:
            raise ServiceError(422, "unknown_permission", "存在未声明的权限")
        for permission in permissions:
            self.require(permission)


async def role_permissions(session, role):
    catalog = permission_catalog(session)
    if role.all_permissions:
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
    roles = await role_grants(session, user_id)
    grants = set().union(*roles.values())
    if pat is not None:
        grants &= set(pat.permissions)
    if user.password_change_required:
        grants.clear()
    return Authorization(
        user,
        pat,
        "pat" if pat else "trusted",
        grants,
        frozenset(permission_catalog(session)),
        frozenset(roles),
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
    auth.grantable(await load_authorization_for_target(session, target))


async def load_authorization_for_target(session, target):
    # Disabled/forced-change targets still retain their role scope for management checks.
    return set().union(*(await role_grants(session, target.id)).values())


async def role_grants(session, user_id):
    """Load current role grants in one query; credential/account restrictions belong to callers."""
    catalog = permission_catalog(session)
    rows = await session.execute(
        select(Role.id, Role.all_permissions, RolePermission.permission_key)
        .join(UserRole, UserRole.role_id == Role.id)
        .outerjoin(RolePermission, RolePermission.role_id == Role.id)
        .where(UserRole.user_id == user_id)
    )
    roles = {}
    for role_id, everything, permission in rows:
        grants = roles.setdefault(role_id, set())
        if everything:
            grants.update(catalog)
        elif permission in catalog:
            grants.add(permission)
    return roles


async def protect_last_admin(session, user):
    # The admin role is only a lockout guard; it grants nothing beyond its permissions.
    holders = (
        select(User.id)
        .join(UserRole)
        .join(Role)
        .where(Role.key == ADMIN_KEY, User.is_active.is_(True))
    )
    if not user.is_active or await session.scalar(holders.where(User.id == user.id)) is None:
        return
    if await session.scalar(holders.where(User.id != user.id).limit(1)) is None:
        raise ServiceError(409, "last_administrator", "必须保留至少一位启用的管理员")

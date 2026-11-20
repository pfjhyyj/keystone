from uuid import uuid4

from sqlalchemy import delete, select

from keystone.core.errors import ServiceError
from keystone.core.time import utcnow
from keystone.platform.audit.services import writer as audit
from keystone.platform.audit.services.writer import Actor
from keystone.platform.identity.models import (
    Role,
    RolePermission,
    User,
    UserRole,
)
from keystone.platform.identity.services.authorization import ADMIN_KEY, identity_lock


async def initialize_admin(database, passwords, data):
    hashed = await passwords.hash(data.password.get_secret_value())
    async with database.transaction() as session:
        await identity_lock(session, identity_write=True)
        if await session.scalar(select(User.id).limit(1)):
            raise ServiceError(409, "already_initialized", "已有用户，不能重复初始化管理员")
        user = User(
            id=uuid4(),
            username=data.username,
            display_name=data.display_name,
            password_hash=hashed,
            password_change_required=False,
        )
        role = Role(
            key=ADMIN_KEY,
            name="管理员",
            description="内置角色，默认拥有全部权限",
            all_permissions=True,
        )
        session.add_all([user, role])
        await session.flush()
        session.add(UserRole(user_id=user.id, role_id=role.id))
        await audit.record(
            session,
            "identity.admin.initialize",
            Actor("system"),
            "identity.user",
            user.id,
            details={"source": "cli", "username": user.username},
        )
        return user.id


async def reset_admin_role(database):
    """Offline lockout recovery: give the built-in admin role all permissions again."""
    async with database.transaction() as session:
        await identity_lock(session, identity_write=True)
        role = await session.scalar(select(Role).where(Role.key == ADMIN_KEY))
        if role is None:
            raise ServiceError(409, "not_initialized", "尚未初始化管理员")
        previous = sorted(
            await session.scalars(
                select(RolePermission.permission_key).where(RolePermission.role_id == role.id)
            )
        )
        if role.all_permissions and not previous:
            return False
        await session.execute(delete(RolePermission).where(RolePermission.role_id == role.id))
        role.all_permissions, role.updated_at, role.updated_by = True, utcnow(), None
        await audit.record(
            session,
            "identity.role.update",
            Actor("system"),
            "identity.role",
            role.id,
            details={
                "source": "cli",
                "before": {"all_permissions": False, "permissions": previous},
                "after": {"all_permissions": True, "permissions": []},
            },
        )
        return True

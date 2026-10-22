from uuid import uuid4

from sqlalchemy import select

from keystone.core.errors import ServiceError
from keystone.platform.audit.services import writer as audit
from keystone.platform.audit.services.writer import Actor
from keystone.platform.identity.models import (
    Role,
    User,
    UserRole,
)
from keystone.platform.identity.services.authorization import ADMIN_KEY, identity_lock


async def initialize_admin(database, passwords, data):
    hashed = await passwords.hash(data.password.get_secret_value())
    async with database.transaction() as session:
        await identity_lock(session, write=True)
        if await session.scalar(select(User.id).limit(1)):
            raise ServiceError(409, "already_initialized", "已有用户，不能重复初始化管理员")
        user = User(
            id=uuid4(),
            username=data.username,
            display_name=data.display_name,
            password_hash=hashed,
            password_change_required=False,
        )
        role = Role(key=ADMIN_KEY, name="管理员", description="内置角色，拥有所有已声明权限")
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

from sqlalchemy import or_, select

from keystone.platform.identity.models import (
    User,
    UserRole,
)
from keystone.platform.identity.schemas import user_view
from keystone.platform.identity.services.access import AccessContext
from keystone.platform.identity.services.authorization import manageable, protect_last_admin
from keystone.platform.identity.services.credentials import revoke_user_credentials


class UserService:
    def __init__(self, context: AccessContext, passwords):
        self.context = context
        self.passwords = passwords

    async def list_users(self, offset, limit, q):
        async with self.context.access("user:read") as (session, _):
            rows = await session.scalars(
                select(User)
                .where(
                    or_(
                        User.username.icontains(q, autoescape=True),
                        User.display_name.icontains(q, autoescape=True),
                    )
                )
                .order_by(User.username)
                .offset(offset)
                .limit(limit)
            )
            users = rows.all()
            if not users:
                return []
            role_ids = {user.id: [] for user in users}
            assignments = await session.execute(
                select(UserRole.user_id, UserRole.role_id).where(UserRole.user_id.in_(role_ids))
            )
            for user_id, role_id in assignments:
                role_ids[user_id].append(role_id)
            return [user_view(user, role_ids[user.id]) for user in users]

    async def create_user(self, data):
        action = "identity.user.create"
        async with self.context.access("user:create", action=action):
            pass
        hashed = await self.passwords.hash(data.password.get_secret_value())
        async with self.context.access("user:create", identity_write=True, action=action) as (
            session,
            auth,
        ):
            user = User(
                username=data.username,
                display_name=data.display_name,
                password_hash=hashed,
                created_by=auth.user.id,
                updated_by=auth.user.id,
            )
            session.add(user)
            await session.flush()
            await self.context.log(
                session,
                auth,
                action,
                "identity.user",
                user.id,
                {"username": user.username, "display_name": user.display_name},
            )
            return user_view(user)

    async def update_user(self, identifier, data):
        action = "identity.user.update"
        async with self.context.access(
            "user:update",
            identity_write=True,
            action=action,
            target="identity.user",
            target_id=identifier,
        ) as (session, auth):
            user = await self.context.existing(session, User, identifier)
            await manageable(session, auth, user)
            before = user.display_name
            if before == data.display_name:
                return user_view(user)
            user.display_name = data.display_name
            self.context.touch(user, auth)
            await self.context.log(
                session,
                auth,
                action,
                "identity.user",
                identifier,
                {"before": {"display_name": before}, "after": {"display_name": user.display_name}},
            )
            return user_view(user)

    async def set_active(self, identifier, active):
        action = "identity.user.enable" if active else "identity.user.disable"
        async with self.context.access(
            "user:update",
            identity_write=True,
            action=action,
            target="identity.user",
            target_id=identifier,
        ) as (session, auth):
            user = await self.context.existing(session, User, identifier)
            await manageable(session, auth, user)
            if user.is_active == active:
                return user_view(user)
            if not active:
                await protect_last_admin(session, user)
            user.is_active = active
            self.context.touch(user, auth)
            counts = (
                await revoke_user_credentials(session, identifier, tokens=True)
                if not active
                else {}
            )
            await self.context.log(
                session, auth, action, "identity.user", identifier, {"is_active": active, **counts}
            )
            return user_view(user)

    async def reset_password(self, identifier, data):
        action = "identity.password.reset"
        async with self.context.access(
            "user:reset_password", action=action, target="identity.user", target_id=identifier
        ) as (session, auth):
            await manageable(session, auth, await self.context.existing(session, User, identifier))
        hashed = await self.passwords.hash(data.password.get_secret_value())
        async with self.context.access(
            "user:reset_password",
            identity_write=True,
            action=action,
            target="identity.user",
            target_id=identifier,
        ) as (session, auth):
            user = await self.context.existing(session, User, identifier)
            await manageable(session, auth, user)
            user.password_hash, user.password_change_required = hashed, True
            self.context.touch(user, auth)
            counts = await revoke_user_credentials(session, identifier, tokens=True)
            await self.context.log(session, auth, action, "identity.user", identifier, counts)

    async def revoke_credentials(self, identifier):
        action = "identity.credentials.revoke"
        async with self.context.access(
            "user:credentials",
            identity_write=True,
            action=action,
            target="identity.user",
            target_id=identifier,
        ) as (session, auth):
            await manageable(session, auth, await self.context.existing(session, User, identifier))
            counts = await revoke_user_credentials(session, identifier, tokens=True)
            await self.context.log(
                session,
                auth,
                action,
                "identity.user",
                identifier,
                {"user_id": str(identifier), **counts},
            )

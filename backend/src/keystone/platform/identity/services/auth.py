import secrets
from datetime import timedelta

from sqlalchemy import select

from keystone.core.errors import ServiceError
from keystone.core.time import utcnow
from keystone.platform.audit.services import writer as audit
from keystone.platform.audit.services.writer import Actor
from keystone.platform.identity.adapters.security import digest
from keystone.platform.identity.models import (
    LoginSession,
    User,
    UserRole,
)
from keystone.platform.identity.schemas import user_view
from keystone.platform.identity.services.access import AccessContext
from keystone.platform.identity.services.authorization import identity_lock
from keystone.platform.identity.services.credentials import revoke_user_credentials


class AuthService:
    def __init__(self, context: AccessContext, passwords):
        self.context = context
        self.passwords = passwords

    async def login(self, data, *, limiter=None):
        if limiter is not None:
            try:
                limiter.check(data.username)
            except ServiceError:
                await audit.failed(
                    self.context.database,
                    "identity.login",
                    Actor(),
                    None,
                    None,
                    self.context.correlation_id,
                    "login_limited",
                    True,
                )
                raise

        # Hash outside the write transaction; recheck the exact stored hash under lock.
        try:
            async with self.context.database.transaction() as session:
                user = await session.scalar(select(User).where(User.username == data.username))
                encoded = user.password_hash if user else None
            valid = await self.passwords.verify(encoded, data.password.get_secret_value())
            async with self.context.database.transaction() as session:
                await identity_lock(session, write=True)
                user = await session.scalar(select(User).where(User.username == data.username))
                if not valid or user is None or not user.is_active or user.password_hash != encoded:
                    raise ServiceError(401, "invalid_login", "用户名或密码错误")
                secret = "ks_" + secrets.token_urlsafe(32)
                token = LoginSession(
                    user_id=user.id,
                    token_digest=digest(secret),
                    expires_at=utcnow() + timedelta(hours=self.context.settings.session_hours),
                )
                session.add(token)
                await session.flush()
                await audit.record(
                    session,
                    "identity.login",
                    Actor("user", user.id),
                    "identity.session",
                    token.id,
                    correlation_id=self.context.correlation_id,
                )
                return secret, user_view(user)
        except Exception as error:
            await audit.failed(
                self.context.database,
                "identity.login",
                Actor(),
                None,
                None,
                self.context.correlation_id,
                "invalid_login" if isinstance(error, ServiceError) else "internal_error",
                False,
            )
            raise

    async def me(self):
        async with self.context.access(allow_password=True) as (session, auth):
            roles = list(
                await session.scalars(
                    select(UserRole.role_id).where(UserRole.user_id == auth.user.id)
                )
            )
            return {
                "user": user_view(auth.user, roles),
                "permissions": sorted(auth.permissions),
                "is_administrator": auth.administrator,
                "credential_kind": auth.kind,
            }

    async def logout(self):
        async with self.context.access(
            write=True, action="identity.logout", session_only=True, allow_password=True
        ) as (session, auth):
            auth.credential.revoked_at = utcnow()
            await self.context.log(
                session,
                auth,
                "identity.logout",
                "identity.session",
                auth.credential.id,
                {"user_id": str(auth.user.id)},
            )

    async def change_password(self, data):
        action = "identity.password.change"
        async with self.context.access(session_only=True, allow_password=True, action=action) as (
            _,
            auth,
        ):
            encoded = auth.user.password_hash
        valid = await self.passwords.verify(encoded, data.current_password.get_secret_value())
        hashed = await self.passwords.hash(data.password.get_secret_value()) if valid else None
        async with self.context.access(
            write=True, session_only=True, allow_password=True, action=action
        ) as (session, auth):
            if not valid or auth.user.password_hash != encoded:
                raise ServiceError(400, "incorrect_password", "当前密码不正确，请重试")
            if data.current_password.get_secret_value() == data.password.get_secret_value():
                raise ServiceError(422, "password_unchanged", "新密码不能与当前密码相同")
            auth.user.password_hash = hashed
            auth.user.password_change_required = False
            self.context.touch(auth.user, auth)
            counts = await revoke_user_credentials(session, auth.user.id)
            await self.context.log(session, auth, action, "identity.user", auth.user.id, counts)

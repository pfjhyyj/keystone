from contextlib import asynccontextmanager

from sqlalchemy.exc import IntegrityError

from keystone.core.errors import ServiceError
from keystone.core.time import utcnow
from keystone.platform.audit.services import writer as audit
from keystone.platform.audit.services.writer import Actor
from keystone.platform.identity.services.authorization import authenticate, identity_lock


class AccessContext:
    def __init__(self, database, settings, credential=None, correlation_id=None):
        self.database, self.settings = database, settings
        self.credential, self.correlation_id = credential, correlation_id

    async def authorize(self, session):
        return await authenticate(session, self.credential)

    @asynccontextmanager
    async def access(
        self,
        permission=None,
        *,
        identity_write=False,
        action=None,
        target=None,
        target_id=None,
        session_only=False,
        allow_password=False,
    ):
        actor = Actor()
        credential_id = None
        try:
            async with self.database.transaction() as session:
                await identity_lock(session, identity_write=identity_write)
                auth = await self.authorize(session)
                actor = Actor("user", auth.user.id)
                credential_id = auth.credential.id if auth.kind == "pat" else None
                if session_only and auth.kind != "session":
                    raise ServiceError(403, "session_required", "此操作仅允许登录会话")
                if auth.user.password_change_required and (
                    not allow_password or auth.kind != "session"
                ):
                    raise ServiceError(403, "password_change_required", "请先修改初始密码")
                if permission:
                    auth.require(permission)
                yield session, auth
        except Exception as error:
            if isinstance(error, IntegrityError):
                error = ServiceError(409, "conflict", "记录已存在或仍被引用")
            if action:
                await audit.failed(
                    self.database,
                    action,
                    actor,
                    target,
                    target_id,
                    self.correlation_id,
                    error.code if isinstance(error, ServiceError) else "internal_error",
                    isinstance(error, ServiceError) and error.status in {401, 403},
                    credential_id=credential_id,
                )
            raise error from None

    async def log(self, session, auth, action, target, target_id, details=None):
        details = dict(details or {})
        if auth.kind == "pat":
            details["credential_id"] = str(auth.credential.id)
        await audit.record(
            session,
            action,
            Actor("user", auth.user.id),
            target,
            target_id,
            correlation_id=self.correlation_id,
            details=details,
        )

    async def existing(self, session, model, identifier):
        value = await session.get(model, identifier)
        if value is None:
            raise ServiceError(404, "not_found", "记录不存在")
        return value

    def touch(self, entity, auth):
        entity.updated_at, entity.updated_by = utcnow(), auth.user.id

import secrets
from datetime import timedelta

from sqlalchemy import select

from keystone.core.errors import ServiceError
from keystone.core.time import utcnow
from keystone.platform.identity.adapters.security import digest
from keystone.platform.identity.models import (
    PersonalAccessToken,
)
from keystone.platform.identity.schemas import token_view
from keystone.platform.identity.services.access import AccessContext


class TokenService:
    def __init__(self, context: AccessContext):
        self.context = context

    async def list_tokens(self, offset, limit):
        async with self.context.access(session_only=True) as (session, auth):
            rows = await session.scalars(
                select(PersonalAccessToken)
                .where(PersonalAccessToken.user_id == auth.user.id)
                .order_by(PersonalAccessToken.created_at.desc(), PersonalAccessToken.id)
                .offset(offset)
                .limit(limit)
            )
            return [token_view(row) for row in rows]

    async def issue_token(self, data):
        action = "identity.pat.issue"
        async with self.context.access(identity_write=True, session_only=True, action=action) as (
            session,
            auth,
        ):
            auth.grantable(data.permissions)
            if (
                not utcnow()
                < data.expires_at
                <= utcnow() + timedelta(days=self.context.settings.pat_max_days)
            ):
                raise ServiceError(422, "invalid_expiry", "令牌有效期须在允许范围内")
            secret = "kp_" + secrets.token_urlsafe(32)
            token = PersonalAccessToken(
                user_id=auth.user.id,
                name=data.name,
                token_digest=digest(secret),
                permissions=sorted(data.permissions),
                expires_at=data.expires_at,
            )
            session.add(token)
            await session.flush()
            await self.context.log(
                session,
                auth,
                action,
                "identity.pat",
                token.id,
                {"user_id": str(auth.user.id), "permissions": token.permissions},
            )
            return {**token_view(token), "token": secret}

    async def revoke_token(self, identifier):
        action = "identity.pat.revoke"
        async with self.context.access(
            identity_write=True,
            session_only=True,
            action=action,
            target="identity.pat",
            target_id=identifier,
        ) as (session, auth):
            token = await self.context.existing(session, PersonalAccessToken, identifier)
            if token.user_id != auth.user.id:
                raise ServiceError(404, "not_found", "记录不存在")
            if token.revoked_at is not None:
                return
            token.revoked_at = utcnow()
            await self.context.log(
                session, auth, action, "identity.pat", identifier, {"user_id": str(token.user_id)}
            )

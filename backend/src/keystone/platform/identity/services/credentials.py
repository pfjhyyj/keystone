from sqlalchemy import update

from keystone.core.time import utcnow
from keystone.platform.identity.models import (
    LoginSession,
    PersonalAccessToken,
)


async def revoke_user_credentials(session, user_id, *, tokens=False):
    now = utcnow()
    sessions = await session.execute(
        update(LoginSession)
        .where(LoginSession.user_id == user_id, LoginSession.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    counts = {"sessions_revoked": sessions.rowcount}
    if tokens:
        result = await session.execute(
            update(PersonalAccessToken)
            .where(PersonalAccessToken.user_id == user_id, PersonalAccessToken.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        counts["tokens_revoked"] = result.rowcount
    return counts

from sqlalchemy import select

from keystone.core.errors import ServiceError
from keystone.core.time_range import history_range
from keystone.platform.audit.models import AuditLog
from keystone.platform.audit.schemas import view
from keystone.platform.identity.services.access import AccessContext


class AuditService:
    def __init__(self, identity: AccessContext):
        self.identity = identity

    async def listing(
        self,
        *,
        since=None,
        until=None,
        actor_id=None,
        actor_type=None,
        action=None,
        target_type=None,
        target_id=None,
        outcome=None,
        offset=0,
        limit=50,
    ):
        async with self.identity.access("audit:read") as (session, auth):
            if not auth.administrator:
                raise ServiceError(403, "administrator_required", "只有内置管理员可查询全局审计")
            since, until = await history_range(session, since, until, error_code="invalid_range")
            statement = select(AuditLog).where(
                AuditLog.occurred_at >= since, AuditLog.occurred_at <= until
            )
            for key, value in {
                "actor_id": actor_id,
                "actor_type": actor_type,
                "action": action,
                "target_type": target_type,
                "target_id": target_id,
                "outcome": outcome,
            }.items():
                if value is not None:
                    statement = statement.where(getattr(AuditLog, key) == value)
            rows = await session.scalars(
                statement.order_by(AuditLog.occurred_at.desc(), AuditLog.id.desc())
                .offset(offset)
                .limit(limit)
            )
            return [view(row) for row in rows]

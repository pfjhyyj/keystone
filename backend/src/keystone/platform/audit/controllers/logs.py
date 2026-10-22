from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Query, Request

from keystone.core.http import Page
from keystone.platform.audit.services.query import AuditService
from keystone.platform.identity.controllers.dependencies import access_context

router = APIRouter(prefix="/api/v1/audit-logs", tags=["audit"])


def _service(request: Request) -> AuditService:
    return AuditService(access_context(request))


@router.get("")
async def logs(
    request: Request,
    page: Page,
    since: datetime | None = None,
    until: datetime | None = None,
    actor_id: UUID | None = None,
    actor_type: Literal["user", "system", "anonymous"] | None = None,
    action: str | None = Query(None, max_length=100),
    target_type: str | None = Query(None, max_length=100),
    target_id: str | None = Query(None, max_length=128),
    outcome: Literal["success", "failure", "denied"] | None = None,
):
    return await _service(request).listing(
        since=since,
        until=until,
        actor_id=actor_id,
        actor_type=actor_type,
        action=action,
        target_type=target_type,
        target_id=target_id,
        outcome=outcome,
        offset=page.offset,
        limit=page.limit,
    )

from uuid import UUID

from fastapi import APIRouter, Query, Request

from keystone.platform.files.services.references import FileReferenceService
from keystone.platform.identity.dependencies import access_context

router = APIRouter(prefix="/api/v1/files", tags=["files"])


def _service(request: Request) -> FileReferenceService:
    return FileReferenceService(access_context(request))


@router.get("/{identifier}/references")
async def references(
    identifier: UUID,
    request: Request,
    offset: int = Query(0, ge=0, le=100000),
    limit: int = Query(50, ge=1, le=100),
):
    return await _service(request).listing(identifier, offset, limit)

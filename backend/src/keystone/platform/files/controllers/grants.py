from uuid import UUID

from fastapi import APIRouter, Query, Request

from keystone.platform.files import schemas
from keystone.platform.files.services.grants import FileGrantService
from keystone.platform.identity.dependencies import access_context

router = APIRouter(prefix="/api/v1/files", tags=["files"])


def _service(request: Request) -> FileGrantService:
    return FileGrantService(access_context(request))


@router.get("/{identifier}/grants")
async def grants(
    identifier: UUID,
    request: Request,
    offset: int = Query(0, ge=0, le=100000),
    limit: int = Query(50, ge=1, le=100),
):
    return await _service(request).listing(identifier, offset, limit)


@router.post("/{identifier}/grants", status_code=201)
async def grant(identifier: UUID, data: schemas.Grant, request: Request):
    return await _service(request).grant(identifier, data)


@router.delete("/{identifier}/grants/{grant_id}", status_code=204)
async def revoke(identifier: UUID, grant_id: UUID, request: Request):
    await _service(request).grant(identifier, grant_id=grant_id)

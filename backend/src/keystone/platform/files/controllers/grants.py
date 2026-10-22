from uuid import UUID

from fastapi import APIRouter, Request

from keystone.core.http import Page
from keystone.platform.files import schemas
from keystone.platform.files.services.grants import FileGrantService
from keystone.platform.identity.controllers.dependencies import access_context

router = APIRouter(prefix="/api/v1/files", tags=["files"])


def _service(request: Request) -> FileGrantService:
    return FileGrantService(access_context(request))


@router.get("/{identifier}/grants")
async def grants(identifier: UUID, request: Request, page: Page):
    return await _service(request).listing(identifier, page.offset, page.limit)


@router.post("/{identifier}/grants", status_code=201)
async def grant(identifier: UUID, data: schemas.Grant, request: Request):
    return await _service(request).grant(identifier, data)


@router.delete("/{identifier}/grants/{grant_id}", status_code=204)
async def revoke(identifier: UUID, grant_id: UUID, request: Request):
    await _service(request).grant(identifier, grant_id=grant_id)

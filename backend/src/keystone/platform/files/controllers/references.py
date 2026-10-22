from uuid import UUID

from fastapi import APIRouter, Request

from keystone.core.http import Page
from keystone.platform.files.services.references import FileReferenceService
from keystone.platform.identity.dependencies import access_context

router = APIRouter(prefix="/api/v1/files", tags=["files"])


def _service(request: Request) -> FileReferenceService:
    return FileReferenceService(access_context(request))


@router.get("/{identifier}/references")
async def references(identifier: UUID, request: Request, page: Page):
    return await _service(request).listing(identifier, page.offset, page.limit)

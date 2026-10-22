from uuid import UUID

from fastapi import APIRouter, Request

from keystone.core.http import Page
from keystone.platform.identity.dependencies import access_context
from keystone.platform.model_access import schemas
from keystone.platform.model_access.services.entries import ModelEntryService

router = APIRouter(prefix="/api/v1", tags=["models"])


def _service(request: Request) -> ModelEntryService:
    return ModelEntryService(access_context(request), request.app.state.runtime.models)


@router.get("/model-entries")
async def entries(request: Request, page: Page):
    return await _service(request).entries(page.offset, page.limit)


@router.post("/model-entries", status_code=201)
async def create_entry(data: schemas.EntryCreate, request: Request):
    return await _service(request).save_entry(data)


@router.patch("/model-entries/{identifier}")
async def edit_entry(identifier: UUID, data: schemas.EntryEdit, request: Request):
    return await _service(request).save_entry(data, identifier)


@router.get("/models")
async def catalog(request: Request, page: Page):
    return await _service(request).catalog(page.offset, page.limit)

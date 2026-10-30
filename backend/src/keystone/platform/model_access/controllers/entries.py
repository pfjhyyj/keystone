from uuid import UUID

from fastapi import APIRouter, Request, Response

from keystone.core.http import Page
from keystone.platform.identity.controllers.dependencies import access_context
from keystone.platform.model_access import schemas
from keystone.platform.model_access.schemas.operations import Delivery, Operation
from keystone.platform.model_access.schemas.pricing import PricingEdit, PricingPreview
from keystone.platform.model_access.services.entries import ModelEntryService
from keystone.platform.model_access.services.pricing import PricingService

router = APIRouter(prefix="/api/v1", tags=["models"])


def _service(request: Request) -> ModelEntryService:
    return ModelEntryService(access_context(request), request.app.state.runtime.models)


@router.get("/model-entries/{identifier}/pricing")
async def entry_pricing(identifier: UUID, request: Request):
    return await PricingService(access_context(request)).get(identifier)


@router.put("/model-entries/{identifier}/pricing")
async def save_pricing(identifier: UUID, data: PricingEdit, request: Request):
    return await PricingService(access_context(request)).save(identifier, data)


@router.post("/model-entries/{identifier}/pricing/preview")
async def preview_pricing(identifier: UUID, data: PricingPreview, request: Request):
    return await PricingService(access_context(request)).preview(identifier, data)


@router.get("/model-entries")
async def entries(request: Request, page: Page):
    return await _service(request).entries(page.offset, page.limit)


@router.post("/model-entries", status_code=201)
async def create_entry(data: schemas.EntryCreate, request: Request):
    return await _service(request).save_entry(data)


@router.patch("/model-entries/{identifier}")
async def edit_entry(identifier: UUID, data: schemas.EntryEdit, request: Request):
    return await _service(request).save_entry(data, identifier)


@router.patch("/model-entries/{identifier}/name")
async def rename_entry(identifier: UUID, data: schemas.NameEdit, request: Request):
    return await _service(request).rename(identifier, data)


@router.delete("/model-entries/{identifier}", status_code=204)
async def delete_entry(identifier: UUID, request: Request):
    await _service(request).delete(identifier)
    return Response(status_code=204)


@router.get("/models")
async def catalog(
    request: Request,
    page: Page,
    operation: Operation = "text.generate",
    delivery: Delivery = "sync",
):
    return await _service(request).catalog(
        page.offset, page.limit, operation=operation, delivery=delivery
    )

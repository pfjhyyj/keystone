from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response

from keystone.core.http import Page
from keystone.platform.identity.controllers.dependencies import access_context
from keystone.platform.model_access import schemas
from keystone.platform.model_access.schemas.cloud_billing import CloudBillQuery
from keystone.platform.model_access.services.channels import ChannelService

router = APIRouter(prefix="/api/v1", tags=["models"])


def _service(request: Request) -> ChannelService:
    return ChannelService(access_context(request), request.app.state.runtime.models)


@router.get("/model-access/options")
async def options(request: Request):
    return await _service(request).options()


@router.get("/model-channels")
async def channels(request: Request, page: Page):
    return await _service(request).channels(page.offset, page.limit)


@router.post("/model-channels", status_code=201)
async def create_channel(data: schemas.ChannelCreate, request: Request):
    return await _service(request).save_channel(data)


@router.patch("/model-channels/{identifier}")
async def edit_channel(identifier: UUID, data: schemas.ChannelEdit, request: Request):
    return await _service(request).save_channel(data, identifier)


@router.patch("/model-channels/{identifier}/name")
async def rename_channel(identifier: UUID, data: schemas.NameEdit, request: Request):
    return await _service(request).rename(identifier, data)


@router.delete("/model-channels/{identifier}", status_code=204)
async def delete_channel(identifier: UUID, request: Request):
    await _service(request).delete(identifier)
    return Response(status_code=204)


@router.post("/model-channels/{identifier}/balance")
async def balance(identifier: UUID, request: Request):
    return await _service(request).refresh_balance(identifier)


@router.get("/model-channels/{identifier}/models")
async def upstream_models(identifier: UUID, request: Request):
    return await _service(request).upstream_models(identifier)


@router.get("/model-channels/{identifier}/bills")
async def cloud_bills(identifier: UUID, data: Annotated[CloudBillQuery, Query()], request: Request):
    return await _service(request).cloud_bills(identifier, data)

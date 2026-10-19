from uuid import UUID

from fastapi import APIRouter, Query, Request

from keystone.platform.identity.dependencies import access_context
from keystone.platform.model_access import schemas
from keystone.platform.model_access.services.channels import ChannelService

router = APIRouter(prefix="/api/v1", tags=["models"])


def _service(request: Request) -> ChannelService:
    return ChannelService(access_context(request), request.app.state.runtime.models)


@router.get("/model-access/options")
async def options(request: Request):
    return await _service(request).options()


@router.get("/model-channels")
async def channels(
    request: Request, offset: int = Query(0, ge=0, le=100000), limit: int = Query(50, ge=1, le=100)
):
    return await _service(request).channels(offset, limit)


@router.post("/model-channels", status_code=201)
async def create_channel(data: schemas.ChannelCreate, request: Request):
    return await _service(request).save_channel(data)


@router.patch("/model-channels/{identifier}")
async def edit_channel(identifier: UUID, data: schemas.ChannelEdit, request: Request):
    return await _service(request).save_channel(data, identifier)


@router.post("/model-channels/{identifier}/balance")
async def balance(identifier: UUID, request: Request):
    return await _service(request).refresh_balance(identifier)

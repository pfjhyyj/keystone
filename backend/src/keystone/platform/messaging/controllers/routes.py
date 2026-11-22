from uuid import UUID

from fastapi import APIRouter, Request

from keystone.core.http import Page
from keystone.platform.identity.controllers.dependencies import access_context
from keystone.platform.messaging import schemas
from keystone.platform.messaging.services.channels import ChannelService
from keystone.platform.messaging.services.deliveries import DeliveryService

router = APIRouter(prefix="/api/v1/messaging", tags=["messaging"])


def _channels(request: Request) -> ChannelService:
    return ChannelService(access_context(request), request.app.state.runtime.messaging_vault())


@router.get("/channels")
async def channels(request: Request, page: Page):
    return await _channels(request).channels(page.offset, page.limit)


@router.post("/channels", status_code=201)
async def create_channel(data: schemas.ChannelCreate, request: Request):
    return await _channels(request).save_channel(data)


@router.patch("/channels/{identifier}")
async def edit_channel(identifier: UUID, data: schemas.ChannelEdit, request: Request):
    return await _channels(request).save_channel(data, identifier)


@router.get("/destinations")
async def destinations(request: Request, page: Page):
    return await _channels(request).destinations(page.offset, page.limit)


@router.post("/destinations", status_code=201)
async def create_destination(data: schemas.DestinationCreate, request: Request):
    return await _channels(request).save_destination(data)


@router.patch("/destinations/{identifier}")
async def edit_destination(identifier: UUID, data: schemas.DestinationEdit, request: Request):
    return await _channels(request).save_destination(data, identifier)


@router.post("/destinations/{identifier}/test", status_code=202)
async def test_destination(identifier: UUID, request: Request):
    return await _channels(request).test(identifier)


@router.get("/deliveries")
async def deliveries(request: Request, page: Page):
    return await DeliveryService(access_context(request)).listing(page.offset, page.limit)

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response

from keystone.core.http import ListTotal, Page
from keystone.platform.identity.controllers.dependencies import access_context
from keystone.platform.messaging import schemas
from keystone.platform.messaging.services.accounts import AccountService
from keystone.platform.messaging.services.channels import ChannelService
from keystone.platform.messaging.services.deliveries import DeliveryService
from keystone.platform.messaging.services.inbound import InboundService, SelfBindingService

router = APIRouter(prefix="/api/v1/messaging", tags=["messaging"])


def _channels(request: Request) -> ChannelService:
    return ChannelService(access_context(request), request.app.state.runtime.messaging_vault())


@router.get("/channels")
async def channels(request: Request, page: Page, total: ListTotal):
    return await _channels(request).channels(page.offset, page.limit, total=total)


@router.post("/channels", status_code=201)
async def create_channel(data: schemas.ChannelCreate, request: Request):
    return await _channels(request).save_channel(data)


@router.patch("/channels/{identifier}")
async def edit_channel(identifier: UUID, data: schemas.ChannelEdit, request: Request):
    return await _channels(request).save_channel(data, identifier)


@router.delete("/channels/{identifier}", status_code=204)
async def delete_channel(identifier: UUID, request: Request):
    await _channels(request).delete_channel(identifier)
    return Response(status_code=204)


@router.get("/channels/{identifier}")
async def channel(identifier: UUID, request: Request):
    return await _channels(request).channel(identifier)


@router.get("/channels/{identifier}/chats")
async def channel_chats(
    identifier: UUID,
    request: Request,
    page_token: Annotated[str | None, Query(max_length=256)] = None,
):
    return await _channels(request).chats(identifier, page_token)


@router.get("/destinations")
async def destinations(
    request: Request,
    page: Page,
    total: ListTotal,
    provider: Literal["feishu_webhook", "feishu_app"] | None = None,
    channel_id: UUID | None = None,
):
    return await _channels(request).destinations(
        page.offset, page.limit, provider, channel_id, total=total
    )


@router.post("/destinations", status_code=201)
async def create_destination(data: schemas.DestinationCreate, request: Request):
    return await _channels(request).save_destination(data)


@router.patch("/destinations/{identifier}")
async def edit_destination(identifier: UUID, data: schemas.DestinationEdit, request: Request):
    return await _channels(request).save_destination(data, identifier)


@router.put("/destinations/{identifier}/executor")
async def set_destination_executor(
    identifier: UUID, data: schemas.DestinationExecutor, request: Request
):
    return await _channels(request).set_executor(identifier, data.user_id)


@router.delete("/destinations/{identifier}", status_code=204)
async def delete_destination(identifier: UUID, request: Request):
    await _channels(request).delete_destination(identifier)
    return Response(status_code=204)


@router.post("/destinations/{identifier}/test", status_code=202)
async def test_destination(identifier: UUID, request: Request, card: bool = False):
    # card=true sends the test card whose button checks the inbound path.
    return await _channels(request).test(identifier, card=card)


def _accounts(request: Request) -> AccountService:
    return AccountService(access_context(request), request.app.state.runtime.messaging_vault())


@router.get("/accounts")
async def accounts(request: Request, page: Page, total: ListTotal, channel_id: UUID | None = None):
    return await _accounts(request).accounts(page.offset, page.limit, channel_id, total=total)


# POST keeps the email or mobile out of URLs and request logs.
@router.post("/accounts/lookup")
async def lookup_account(data: schemas.ContactLookup, request: Request):
    return await _accounts(request).lookup(data)


@router.post("/accounts", status_code=201)
async def bind_account(data: schemas.AccountCreate, request: Request):
    return await _accounts(request).bind(data)


@router.delete("/accounts/{identifier}", status_code=204)
async def unbind_account(identifier: UUID, request: Request):
    await _accounts(request).unbind(identifier)
    return Response(status_code=204)


@router.post("/accounts/{identifier}/test", status_code=202)
async def test_account(identifier: UUID, request: Request, card: bool = False):
    return await _accounts(request).test(identifier, card=card)


@router.get("/inbounds")
async def inbounds(
    request: Request,
    page: Page,
    total: ListTotal,
    channel_id: UUID | None = None,
    status: Literal["received", "processed", "rejected", "expired", "failed"] | None = None,
):
    return await InboundService(access_context(request)).listing(
        page.offset, page.limit, channel_id, status, total=total
    )


# Any signed-in user manages their own bindings; a binding is made by messaging a code.
@router.get("/me/accounts")
async def my_accounts(request: Request):
    return await SelfBindingService(access_context(request)).accounts()


@router.delete("/me/accounts/{identifier}", status_code=204)
async def unbind_mine(identifier: UUID, request: Request):
    await SelfBindingService(access_context(request)).unbind(identifier)
    return Response(status_code=204)


@router.post("/bind-codes", status_code=201)
async def create_bind_code(request: Request):
    return await SelfBindingService(access_context(request)).create()


@router.get("/deliveries")
async def deliveries(
    request: Request,
    page: Page,
    total: ListTotal,
    channel_id: UUID | None = None,
    status: Literal["pending", "cancelled", "sending", "succeeded", "failed", "unknown", "expired"]
    | None = None,
):
    return await DeliveryService(access_context(request)).listing(
        page.offset, page.limit, channel_id, status, total=total
    )

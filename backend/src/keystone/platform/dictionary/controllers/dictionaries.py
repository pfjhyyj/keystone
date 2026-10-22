from uuid import UUID

from fastapi import APIRouter, Request

from keystone.core.http import Page
from keystone.platform.dictionary import schemas
from keystone.platform.dictionary.services.dictionaries import DictionaryService
from keystone.platform.identity.dependencies import access_context

router = APIRouter(prefix="/api/v1/dictionaries", tags=["dictionary"])


def _service(request: Request) -> DictionaryService:
    return DictionaryService(access_context(request))


@router.get("")
async def listing(request: Request, page: Page):
    return await _service(request).listing(page.offset, page.limit)


@router.post("", status_code=201)
async def create(data: schemas.DictionaryCreate, request: Request):
    return await _service(request).save(data)


@router.patch("/{identifier}")
async def edit(identifier: UUID, data: schemas.DictionaryEdit, request: Request):
    return await _service(request).save(data, identifier=identifier)


@router.get("/by-key/{key}/options")
async def options(key: str, request: Request, page: Page):
    return await _service(request).options(key, page.offset, page.limit)


@router.get("/by-key/{key}/items/{identifier}")
async def resolve(key: str, identifier: UUID, request: Request):
    return await _service(request).resolve(key, identifier)


@router.get("/{identifier}/items")
async def items(identifier: UUID, request: Request, page: Page):
    return await _service(request).items(identifier, page.offset, page.limit)


@router.post("/{identifier}/items", status_code=201)
async def create_item(identifier: UUID, data: schemas.ItemCreate, request: Request):
    return await _service(request).save(data, parent_id=identifier)


@router.patch("/{parent_id}/items/{identifier}")
async def edit_item(parent_id: UUID, identifier: UUID, data: schemas.ItemEdit, request: Request):
    return await _service(request).save(data, identifier=identifier, parent_id=parent_id)

from uuid import UUID

from fastapi import APIRouter, Query, Request

from keystone.platform.dictionary import schemas
from keystone.platform.dictionary.services.dictionaries import DictionaryService
from keystone.platform.identity.dependencies import access_context

router = APIRouter(prefix="/api/v1/dictionaries", tags=["dictionary"])


def _service(request: Request) -> DictionaryService:
    return DictionaryService(access_context(request))


@router.get("")
async def listing(
    request: Request, offset: int = Query(0, ge=0, le=100000), limit: int = Query(50, ge=1, le=100)
):
    return await _service(request).listing(offset, limit)


@router.post("", status_code=201)
async def create(data: schemas.DictionaryCreate, request: Request):
    return await _service(request).save(data)


@router.patch("/{identifier}")
async def edit(identifier: UUID, data: schemas.DictionaryEdit, request: Request):
    return await _service(request).save(data, identifier=identifier)


@router.get("/by-key/{key}/options")
async def options(
    key: str,
    request: Request,
    offset: int = Query(0, ge=0, le=100000),
    limit: int = Query(50, ge=1, le=100),
):
    return await _service(request).options(key, offset, limit)


@router.get("/by-key/{key}/items/{identifier}")
async def resolve(key: str, identifier: UUID, request: Request):
    return await _service(request).resolve(key, identifier)


@router.get("/{identifier}/items")
async def items(
    identifier: UUID,
    request: Request,
    offset: int = Query(0, ge=0, le=100000),
    limit: int = Query(50, ge=1, le=100),
):
    return await _service(request).items(identifier, offset, limit)


@router.post("/{identifier}/items", status_code=201)
async def create_item(identifier: UUID, data: schemas.ItemCreate, request: Request):
    return await _service(request).save(data, parent_id=identifier)


@router.patch("/{parent_id}/items/{identifier}")
async def edit_item(parent_id: UUID, identifier: UUID, data: schemas.ItemEdit, request: Request):
    return await _service(request).save(data, identifier=identifier, parent_id=parent_id)

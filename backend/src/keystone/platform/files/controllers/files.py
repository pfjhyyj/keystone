import asyncio
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request

from keystone.core.errors import ServiceError
from keystone.platform.files import schemas
from keystone.platform.files.controllers.streaming import ContentResponse
from keystone.platform.files.services.files import FileService
from keystone.platform.identity.dependencies import access_context

router = APIRouter(prefix="/api/v1/files", tags=["files"])


def _service(request: Request) -> FileService:
    return FileService(access_context(request), request.app.state.runtime.storage)


@router.get("")
async def listing(
    request: Request, offset: int = Query(0, ge=0, le=100000), limit: int = Query(50, ge=1, le=100)
):
    return await _service(request).listing(offset, limit)


@router.get("/options")
async def options(request: Request):
    return await _service(request).options()


@router.post("", status_code=201)
async def upload(request: Request, data: Annotated[schemas.Upload, Query()]):
    target = _service(request)
    await target.prepare_upload(data.expected_size, data.storage_key)
    # Raw binary body avoids unbounded multipart spooling before authentication.
    try:
        async with asyncio.timeout(request.app.state.runtime.settings.file_upload_timeout_seconds):
            async with request.app.state.upload_slots:
                return await target.upload_body(data, request.stream())
    except TimeoutError:
        raise ServiceError(408, "upload_timeout", "上传超时，请重试") from None


@router.get("/{identifier}")
async def detail(identifier: UUID, request: Request):
    return await _service(request).detail(identifier)


@router.api_route("/{identifier}/content", methods=["GET", "HEAD"])
async def content(identifier: UUID, request: Request):
    return ContentResponse(_service(request).open_content(identifier), request)


@router.patch("/{identifier}")
async def edit(identifier: UUID, data: schemas.Edit, request: Request):
    return await _service(request).edit(identifier, data)


@router.delete("/{identifier}", status_code=202)
async def delete_file(identifier: UUID, request: Request):
    return await _service(request).request_delete(identifier)

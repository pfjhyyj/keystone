import asyncio
from typing import Annotated
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response

from keystone.core.errors import ServiceError
from keystone.platform.files import schemas
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
    async with request.app.state.upload_slots:
        try:
            async with asyncio.timeout(60):
                content = bytearray()
                async for chunk in request.stream():
                    if len(content) + len(chunk) > data.expected_size:
                        raise ServiceError(413, "file_too_large", "上传内容超过声明大小")
                    content.extend(chunk)
                return await target.upload(data, bytes(content))
        except TimeoutError:
            raise ServiceError(408, "upload_timeout", "上传超时，请重试") from None


@router.get("/{identifier}")
async def detail(identifier: UUID, request: Request):
    return await _service(request).detail(identifier)


@router.get("/{identifier}/content")
async def content(identifier: UUID, request: Request):
    name, body = await _service(request).content(identifier)
    return Response(
        body,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name, safe='')}",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox",
        },
    )


@router.patch("/{identifier}")
async def edit(identifier: UUID, data: schemas.Edit, request: Request):
    return await _service(request).edit(identifier, data)


@router.delete("/{identifier}", status_code=202)
async def delete_file(identifier: UUID, request: Request):
    return await _service(request).request_delete(identifier)

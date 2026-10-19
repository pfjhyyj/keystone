from uuid import UUID

from fastapi import APIRouter, Query, Request

from keystone.platform.identity import schemas
from keystone.platform.identity.dependencies import (
    access_context,
)
from keystone.platform.identity.services.tokens import TokenService

router = APIRouter(prefix="/api/v1", tags=["identity"])


def _service(request: Request) -> TokenService:
    return TokenService(access_context(request))


@router.get("/auth/tokens")
async def tokens(
    request: Request, offset: int = Query(0, ge=0, le=100000), limit: int = Query(50, ge=1, le=100)
):
    return await _service(request).list_tokens(offset, limit)


@router.post("/auth/tokens", status_code=201)
async def issue_token(data: schemas.TokenCreate, request: Request):
    return await _service(request).issue_token(data)


@router.delete("/auth/tokens/{identifier}", status_code=204)
async def revoke_token(identifier: UUID, request: Request):
    await _service(request).revoke_token(identifier)

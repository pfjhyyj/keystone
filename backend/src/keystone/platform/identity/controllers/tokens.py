from uuid import UUID

from fastapi import APIRouter, Request

from keystone.core.http import Page
from keystone.platform.identity import schemas
from keystone.platform.identity.controllers.dependencies import access_context
from keystone.platform.identity.services.tokens import TokenService

router = APIRouter(prefix="/api/v1", tags=["identity"])


def _service(request: Request) -> TokenService:
    return TokenService(access_context(request))


@router.get("/auth/tokens")
async def tokens(request: Request, page: Page):
    return await _service(request).list_tokens(page.offset, page.limit)


@router.post("/auth/tokens", status_code=201)
async def issue_token(data: schemas.TokenCreate, request: Request):
    return await _service(request).issue_token(data)


@router.delete("/auth/tokens/{identifier}", status_code=204)
async def revoke_token(identifier: UUID, request: Request):
    await _service(request).revoke_token(identifier)

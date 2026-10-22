from uuid import UUID

from fastapi import APIRouter, Query, Request

from keystone.core.http import Page
from keystone.platform.identity import schemas
from keystone.platform.identity.controllers.dependencies import access_context
from keystone.platform.identity.services.users import UserService

router = APIRouter(prefix="/api/v1", tags=["identity"])


def _service(request: Request) -> UserService:
    return UserService(access_context(request), request.app.state.passwords)


@router.get("/users")
async def users(request: Request, page: Page, q: str = Query("", max_length=128)):
    return await _service(request).list_users(page.offset, page.limit, q)


@router.post("/users", status_code=201)
async def create_user(data: schemas.UserCreate, request: Request):
    return await _service(request).create_user(data)


@router.patch("/users/{identifier}")
async def update_user(identifier: UUID, data: schemas.UserUpdate, request: Request):
    return await _service(request).update_user(identifier, data)


@router.put("/users/{identifier}/active")
async def set_active(identifier: UUID, data: schemas.ActiveChange, request: Request):
    return await _service(request).set_active(identifier, data.is_active)


@router.post("/users/{identifier}/password", status_code=204)
async def reset_password(identifier: UUID, data: schemas.PasswordReset, request: Request):
    await _service(request).reset_password(identifier, data)


@router.post("/users/{identifier}/credentials/revoke", status_code=204)
async def revoke_credentials(identifier: UUID, request: Request):
    await _service(request).revoke_credentials(identifier)

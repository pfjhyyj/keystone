from uuid import UUID

from fastapi import APIRouter, Query, Request

from keystone.platform.identity import schemas
from keystone.platform.identity.dependencies import (
    access_context,
)
from keystone.platform.identity.services.roles import RoleService

router = APIRouter(prefix="/api/v1", tags=["identity"])


def _service(request: Request) -> RoleService:
    return RoleService(access_context(request))


@router.get("/permissions")
async def permissions(request: Request):
    return await _service(request).permissions()


@router.get("/roles")
async def roles(
    request: Request, offset: int = Query(0, ge=0, le=100000), limit: int = Query(50, ge=1, le=100)
):
    return await _service(request).list_roles(offset, limit)


@router.post("/roles", status_code=201)
async def create_role(data: schemas.RoleCreate, request: Request):
    return await _service(request).save_role(data)


@router.put("/roles/{identifier}")
async def update_role(identifier: UUID, data: schemas.RoleUpdate, request: Request):
    return await _service(request).save_role(data, identifier)


@router.delete("/roles/{identifier}", status_code=204)
async def delete_role(identifier: UUID, request: Request):
    await request.app.state.runtime.role_deletion(_service(request)).execute(identifier)


@router.put("/users/{user_id}/roles/{role_id}", status_code=204)
async def assignment(user_id: UUID, role_id: UUID, data: schemas.Assignment, request: Request):
    await _service(request).assign_role(user_id, role_id, data.assigned)

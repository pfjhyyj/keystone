from typing import Annotated

from fastapi import APIRouter, Query, Request

from keystone.core.schemas import Input
from keystone.platform.identity.controllers.dependencies import access_context
from keystone.platform.model_access.schemas.types import Key

router = APIRouter(prefix="/api/v1/flow-model-defaults", tags=["flow"])


class DefaultEdit(Input):
    model_key: Key


def _service(request: Request):
    return request.app.state.runtime.flow_model_defaults(access_context(request))


@router.get("")
async def listing(request: Request):
    return await _service(request).listing()


@router.get("/{definition_key}/{role}/models")
async def models(definition_key: str, role: str, request: Request):
    return await _service(request).choices(definition_key, role)


@router.get("/{definition_key}/{role}/current")
async def current(
    definition_key: str,
    role: str,
    request: Request,
    definition_revision: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
):
    return await _service(request).current(
        definition_key, role, definition_revision=definition_revision
    )


@router.put("/{definition_key}/{role}")
async def save(definition_key: str, role: str, data: DefaultEdit, request: Request):
    return await _service(request).save(definition_key, role, data.model_key)

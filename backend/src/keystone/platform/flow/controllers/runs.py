from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Request

from keystone.core.http import Page
from keystone.platform.flow.schemas import Start
from keystone.platform.flow.services.runs import FlowService
from keystone.platform.identity.controllers.dependencies import access_context

router = APIRouter(prefix="/api/v1", tags=["flow"])


def _service(request: Request) -> FlowService:
    return request.app.state.runtime.flow_service(access_context(request))


@router.get("/flow-definitions")
async def definitions(request: Request):
    return await _service(request).definitions()


@router.post("/flow-runs", status_code=202)
async def start(data: Start, request: Request):
    return await _service(request).start(data)


@router.get("/flow-runs")
async def listing(
    request: Request,
    page: Page,
    status: Literal["pending", "running", "succeeded", "failed", "cancelled"] | None = None,
):
    return await _service(request).listing(page.offset, page.limit, status)


@router.get("/flow-runs/{identifier}")
async def detail(identifier: UUID, request: Request):
    return await _service(request).detail(identifier)


@router.post("/flow-runs/{identifier}/cancel")
async def cancel(identifier: UUID, request: Request):
    return await _service(request).cancel(identifier)


@router.get("/flow-runs/{identifier}/steps")
async def steps(identifier: UUID, request: Request):
    return await _service(request).steps(identifier, request.app.state.runtime.flow_engine)


@router.get("/flow-runs/{identifier}/costs")
async def costs(identifier: UUID, request: Request):
    return await request.app.state.runtime.flow_costs(access_context(request)).execute(identifier)

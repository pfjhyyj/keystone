from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response

from keystone.core.content_response import ContentResponse
from keystone.core.http import ListTotal, Page
from keystone.platform.flow.schemas.diagnostics import Diagnostics, DiagnosticSection
from keystone.platform.flow.schemas.step_diagnostics import MAX_ATTACHMENTS, StepAttempts
from keystone.platform.flow.services.runs import FlowService
from keystone.platform.flow.services.step_diagnostics import StepDiagnosticService
from keystone.platform.identity.controllers.dependencies import access_context

router = APIRouter(prefix="/api/v1", tags=["flow"])


def _service(request: Request) -> FlowService:
    return request.app.state.runtime.flow_service(access_context(request))


@router.get("/flow-runs")
async def listing(
    request: Request,
    page: Page,
    total: ListTotal,
    status: Literal["pending", "running", "succeeded", "failed", "cancelled"] | None = None,
):
    return await _service(request).listing(page.offset, page.limit, status, total=total)


@router.get("/flow-runs/{identifier}")
async def detail(identifier: UUID, request: Request):
    return await _service(request).detail(identifier)


@router.post("/flow-runs/{identifier}/cancel")
async def cancel(identifier: UUID, request: Request):
    return await _service(request).cancel(identifier)


@router.get("/flow-runs/{identifier}/steps")
async def steps(identifier: UUID, request: Request):
    return await _service(request).steps(identifier, request.app.state.runtime.flow_engine)


def _step_service(request: Request):
    identity = access_context(request)
    return StepDiagnosticService(identity, request.app.state.runtime.managed_files(identity))


WorkflowId = Annotated[str, Query(min_length=1, max_length=1024)]
StepId = Annotated[int, Query(ge=0, le=2**31 - 1)]


@router.get("/flow-runs/{identifier}/step-attempts", response_model=StepAttempts)
async def step_attempts(
    identifier: UUID, request: Request, response: Response, workflow_id: WorkflowId, step_id: StepId
):
    response.headers["Cache-Control"] = "no-store"
    return await _step_service(request).attempts(identifier, workflow_id, step_id)


@router.get("/flow-runs/{identifier}/step-attachment")
async def step_attachment(
    identifier: UUID,
    request: Request,
    workflow_id: WorkflowId,
    step_id: StepId,
    attempt: Annotated[int, Query(ge=1, le=2**31 - 1)],
    index: Annotated[int, Query(ge=0, lt=MAX_ATTACHMENTS)],
):
    return ContentResponse(
        _step_service(request).open_attachment(identifier, workflow_id, step_id, attempt, index),
        request,
    )


@router.get("/flow-runs/{identifier}/output-file")
async def output_file(
    identifier: UUID,
    request: Request,
    file_id: Annotated[UUID, Query()],
):
    return ContentResponse(
        _step_service(request).open_output_file(identifier, file_id),
        request,
    )


@router.get("/flow-runs/{identifier}/costs")
async def costs(
    identifier: UUID, request: Request, cost_basis: Literal["actual", "estimated"] = "actual"
):
    return await request.app.state.runtime.flow_costs(access_context(request)).execute(
        identifier, cost_basis=cost_basis
    )


@router.get("/flow-runs/{identifier}/diagnostics", response_model=Diagnostics)
async def diagnostics(
    identifier: UUID,
    request: Request,
    page: Page,
    section: DiagnosticSection = "all",
    include_total: bool = False,
):
    return await request.app.state.runtime.flow_diagnostics(access_context(request)).execute(
        identifier, page.offset, page.limit, section, include_total=include_total
    )

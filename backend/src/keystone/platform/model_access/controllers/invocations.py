import json
from contextlib import aclosing
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from keystone.platform.identity.controllers.dependencies import access_context
from keystone.platform.model_access import schemas
from keystone.platform.model_access.schemas.statistics import (
    InvocationFilters,
    InvocationListQuery,
    StatisticsOptionsQuery,
    StatisticsQuery,
)
from keystone.platform.model_access.services.entries import ModelEntryService
from keystone.platform.model_access.services.invocations import InvocationService
from keystone.platform.model_access.services.runtime import ModelRuntime, safe_error
from keystone.platform.model_access.services.statistics import StatisticsService

router = APIRouter(prefix="/api/v1", tags=["models"])


def _service(request: Request) -> InvocationService:
    return InvocationService(access_context(request), request.app.state.runtime.models)


@router.post("/models/invoke")
async def invoke(data: schemas.Invoke, request: Request):
    target = ModelEntryService(access_context(request), request.app.state.runtime.models)
    selected = await target.prepare_invoke(data.model_key)
    runtime = ModelRuntime(target)
    if not data.stream:
        return await runtime.invoke(data, selection=selected)

    async def stream():
        try:
            async with aclosing(runtime.events(data, selection=selected)) as events:
                async for event in events:
                    yield "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
        except Exception as error:
            status, code, detail = safe_error(error)
            yield (
                "data: "
                + json.dumps(
                    {"type": "error", "http_status": status, "code": code, "detail": detail}
                )
                + "\n\n"
            )

    return StreamingResponse(
        stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
    )


@router.get("/model-invocations/summary")
async def summary(request: Request, query: Annotated[InvocationFilters, Query()]):
    return await _service(request).summary(**query.filters())


@router.get("/model-invocations/statistics")
async def statistics(request: Request, query: Annotated[StatisticsQuery, Query()]):
    return await StatisticsService(access_context(request)).statistics(query)


@router.get("/model-invocations/options")
async def statistics_options(request: Request, query: Annotated[StatisticsOptionsQuery, Query()]):
    return await StatisticsService(access_context(request)).options(query)


@router.get("/model-invocations")
async def invocations(request: Request, query: Annotated[InvocationListQuery, Query()]):
    return await _service(request).invocations(query.offset, query.limit, **query.filters())


@router.get("/model-invocations/{identifier}")
async def invocation(identifier: UUID, request: Request):
    return await _service(request).invocation(identifier)


@router.post("/model-invocations/{identifier}/cost")
async def refresh_cost(identifier: UUID, request: Request):
    return await _service(request).refresh_cost(identifier)

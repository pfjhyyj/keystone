from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request

from keystone.platform.identity.controllers.dependencies import access_context
from keystone.platform.model_access.schemas.statistics import (
    InvocationFilters,
    InvocationListQuery,
    StatisticsOptionsQuery,
    StatisticsQuery,
)
from keystone.platform.model_access.services.invocations import InvocationService
from keystone.platform.model_access.services.statistics import StatisticsService

router = APIRouter(prefix="/api/v1", tags=["models"])


def _service(request: Request) -> InvocationService:
    return InvocationService(access_context(request), request.app.state.runtime.models)


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

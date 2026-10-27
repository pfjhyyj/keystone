"""Small safe error/correlation boundary."""

import logging
import re
from http import HTTPStatus
from time import monotonic
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import iter_route_contexts
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException

from keystone.core.errors import ServiceError
from keystone.core.logging import safe_exception


class Pagination:
    """One bounded page contract for every list endpoint."""

    def __init__(
        self,
        offset: int = Query(0, ge=0, le=100000),
        limit: int = Query(50, ge=1, le=100),
    ):
        self.offset, self.limit = offset, limit


Page = Annotated[Pagination, Depends()]


def validate_http_routes(app: FastAPI) -> None:
    """Reject duplicate effective paths, including nested included routers."""
    seen = set()
    for route in iter_route_contexts(app.routes):
        if route.path is None or not route.methods:
            continue
        # Parameter names do not distinguish routes that match the same URL.
        path = re.sub(r"\{[^}:]+", "{", route.path).replace("{:str}", "{}")
        for method in route.methods:
            key = method, path
            if key in seen:
                raise ValueError(f"Duplicate HTTP route: {method} {route.path}")
            seen.add(key)


def failure(request: Request, status: int, code: str, detail: str, headers=None):
    identifier = request.state.request_id
    return JSONResponse(
        status_code=status,
        content={"code": code, "detail": detail, "request_id": identifier},
        headers={**(headers or {}), "X-Request-ID": identifier, "Cache-Control": "no-store"},
    )


class RequestBoundary:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        scope.setdefault("state", {})["request_id"] = uuid4().hex
        started = False
        status_code = 500
        outcome = "interrupted"
        began = monotonic()

        async def correlated_send(message):
            nonlocal started, status_code, outcome
            if message["type"] == "http.response.start":
                started = True
                status_code = message["status"]
                MutableHeaders(scope=message)["X-Request-ID"] = scope["state"]["request_id"]
                MutableHeaders(scope=message)["Cache-Control"] = "no-store"
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                outcome = "completed"

        try:
            await self.app(scope, receive, correlated_send)
        except Exception as error:
            logging.getLogger(__name__).error(
                "request_failed type=%s request_id=%s",
                type(error).__name__,
                scope["state"]["request_id"],
                extra={
                    "request_id": scope["state"]["request_id"],
                    "exception": safe_exception(error),
                },
            )
            if started:
                # Preserve cancellation/cleanup; do not re-raise sensitive exception text.
                raise RuntimeError("Response interrupted") from None
            response = failure(Request(scope), 500, "internal_error", "Internal server error")
            await response(scope, receive, correlated_send)
        finally:
            route = scope.get("route")
            logging.getLogger(__name__).info(
                "http_request",
                extra={
                    "request_id": scope["state"]["request_id"],
                    "diagnostic": {
                        "method": scope["method"],
                        "route": getattr(route, "path_format", "<unmatched>"),
                        "status_code": status_code,
                        "elapsed_ms": round((monotonic() - began) * 1000, 2),
                        "outcome": outcome,
                    },
                },
            )


def install_errors(app: FastAPI) -> None:
    @app.exception_handler(ServiceError)
    async def service_error(request, error):
        headers = {"Retry-After": "60"} if error.status == 429 else None
        return failure(request, error.status, error.code, error.detail, headers)

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request, error):
        return failure(request, 422, "invalid_input", "Invalid request input")

    @app.exception_handler(HTTPException)
    async def http_error(request, error):
        return failure(
            request,
            error.status_code,
            f"http_{error.status_code}",
            HTTPStatus(error.status_code).phrase,
            error.headers,
        )

    app.add_middleware(RequestBoundary)

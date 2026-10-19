"""Small safe error/correlation boundary, adapted from ether_api.errors."""

import logging
from http import HTTPStatus
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException

from keystone.core.errors import ServiceError
from keystone.core.logging import safe_exception


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

        async def correlated_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                MutableHeaders(scope=message)["X-Request-ID"] = scope["state"]["request_id"]
                MutableHeaders(scope=message)["Cache-Control"] = "no-store"
            await send(message)

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

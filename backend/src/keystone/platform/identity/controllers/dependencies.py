"""HTTP authentication and CSRF boundary shared by domain routers."""

import secrets

from fastapi import Request, WebSocket

from keystone.core.errors import ServiceError
from keystone.platform.identity.adapters.security import csrf_token
from keystone.platform.identity.schemas.credentials import Credential
from keystone.platform.identity.services.access import AccessContext


def cookie_name(settings):
    return "__Host-keystone_session" if settings.env == "production" else "keystone_session"


def browser_origin(request):
    settings = request.app.state.runtime.settings
    if request.headers.get("origin") not in settings.browser_origins:
        raise ServiceError(403, "invalid_origin", "请求来源不受信任")
    if request.headers.get("x-keystone-request") != "1":
        raise ServiceError(403, "csrf_required", "缺少请求保护信息")


def access_context(request: Request) -> AccessContext:
    runtime = request.app.state.runtime
    header = request.headers.get("authorization")
    secret = request.cookies.get(cookie_name(runtime.settings))
    credential = None
    if header is not None:
        scheme, _, value = header.partition(" ")
        credential = Credential("pat", value if scheme.lower() == "bearer" else "")
    elif secret:
        credential = Credential("session", secret)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            browser_origin(request)
            if not secrets.compare_digest(
                request.headers.get("x-csrf-token", "").encode(), csrf_token(secret).encode()
            ):
                raise ServiceError(403, "csrf_required", "请求保护信息失效，请刷新页面")
    return AccessContext(
        runtime.database,
        runtime.settings,
        credential,
        request.state.request_id,
    )


def clear_cookie(request, response):
    settings = request.app.state.runtime.settings
    response.delete_cookie(
        cookie_name(settings),
        path="/",
        httponly=True,
        secure=settings.env == "production",
        samesite="lax",
    )


def socket_access_context(socket: WebSocket) -> AccessContext:
    runtime = socket.app.state.runtime
    if socket.headers.get("origin") not in runtime.settings.browser_origins:
        raise ServiceError(403, "invalid_origin", "请求来源不受信任")
    secret = socket.cookies.get(cookie_name(runtime.settings))
    return AccessContext(
        runtime.database,
        runtime.settings,
        Credential("session", secret) if secret else None,
        socket.state.request_id,
    )

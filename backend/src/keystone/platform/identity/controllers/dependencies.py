"""HTTP authentication and CSRF boundary shared by domain routers."""

import logging
import secrets
from math import ceil
from urllib.parse import urlsplit

from fastapi import Request, Response, WebSocket
from starlette.datastructures import MutableHeaders
from starlette.requests import HTTPConnection

from keystone.core.errors import ServiceError
from keystone.core.time import utcnow
from keystone.platform.identity.adapters.security import csrf_token
from keystone.platform.identity.schemas.credentials import Credential
from keystone.platform.identity.services.access import AccessContext
from keystone.platform.identity.services.auth import renew_session

log = logging.getLogger(__name__)


def cookie_name(settings):
    return "__Host-keystone_session" if settings.env == "production" else "keystone_session"


def _origin_tuple(value: str) -> tuple[str, str, int] | None:
    if any(ord(char) <= 32 or char.isspace() or char in "\\?#," for char in value):
        return None
    try:
        url = urlsplit(value)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.path
            or url.username is not None
        ):
            return None
        port = url.port
    except ValueError:
        return None
    host = url.hostname
    authority = f"[{host}]" if ":" in host else host
    if port is not None:
        authority = f"{authority}:{port}"
    if url.netloc.lower() != authority:
        return None
    return url.scheme, host, port if port is not None else (443 if url.scheme == "https" else 80)


def _require_trusted_origin(connection: HTTPConnection):
    origins = connection.headers.getlist("origin")
    hosts = connection.headers.getlist("host")
    if len(origins) == len(hosts) == 1:
        scheme = connection.scope["scheme"]
        scheme = {"ws": "http", "wss": "https"}.get(scheme, scheme)
        # Production HTTPS may terminate at the proxy; it must preserve the public Host.
        if connection.app.state.runtime.settings.env == "production":
            scheme = "https"
        origin = _origin_tuple(origins[0])
        if origin is not None and origin == _origin_tuple(f"{scheme}://{hosts[0]}"):
            return
    raise ServiceError(
        403, "invalid_origin", "请求来源不受信任。请从与 API 同源的页面打开，代理须保留 Host。"
    )


def browser_origin(request):
    _require_trusted_origin(request)
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
    context = AccessContext(
        runtime.database,
        runtime.settings,
        credential,
        request.state.request_id,
    )
    activity = request.headers.get("x-keystone-session-active")
    if (
        credential is not None
        and credential.kind == "session"
        and activity in {"0", "1"}
        and request.headers.get("x-keystone-request") == "1"
        and secrets.compare_digest(
            request.headers.get("x-csrf-token", "").encode(), csrf_token(credential.secret).encode()
        )
    ):
        request.state.session_renewal = (context, activity == "1")
    return context


def set_session_cookie(response, settings, secret, max_age):
    response.set_cookie(
        cookie_name(settings),
        secret,
        path="/",
        httponly=True,
        secure=settings.env == "production",
        samesite="lax",
        max_age=max_age,
    )


class BrowserSessionRenewal:
    def __init__(self, app, has_active_work):
        self.app = app
        self.has_active_work = has_active_work

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_response(message):
            if message["type"] == "http.response.start" and 200 <= message["status"] < 300:
                renewal = scope.get("state", {}).get("session_renewal")
                if renewal is not None:
                    headers = MutableHeaders(scope=message)
                    if "set-cookie" not in headers:
                        await self.renew(headers, *renewal)
            await send(message)

        await self.app(scope, receive, send_response)

    async def renew(self, headers, context, active):
        try:
            expires_at = await renew_session(
                context, active=active, has_active_work=self.has_active_work
            )
        except ServiceError:
            return
        except Exception:
            log.warning("session_renewal_failed request_id=%s", context.correlation_id)
            return
        if expires_at is None:
            return
        remaining = ceil((expires_at - utcnow()).total_seconds())
        if remaining > 0:
            response = Response()
            set_session_cookie(response, context.settings, context.credential.secret, remaining)
            headers.append("set-cookie", response.headers["set-cookie"])


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
    _require_trusted_origin(socket)
    runtime = socket.app.state.runtime
    secret = socket.cookies.get(cookie_name(runtime.settings))
    return AccessContext(
        runtime.database,
        runtime.settings,
        Credential("session", secret) if secret else None,
        socket.state.request_id,
    )

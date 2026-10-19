from fastapi import APIRouter, Request, Response

from keystone.platform.identity import schemas
from keystone.platform.identity.access import AccessContext
from keystone.platform.identity.dependencies import (
    access_context,
    browser_origin,
    clear_cookie,
    cookie_name,
)
from keystone.platform.identity.security import csrf_token
from keystone.platform.identity.services.auth import AuthService

router = APIRouter(prefix="/api/v1", tags=["identity"])


def _service(request: Request) -> AuthService:
    return AuthService(access_context(request), request.app.state.passwords)


@router.post("/auth/login")
async def login(data: schemas.Login, request: Request, response: Response):
    browser_origin(request)
    runtime = request.app.state.runtime
    svc = AuthService(
        AccessContext(runtime.database, runtime.settings, correlation_id=request.state.request_id),
        request.app.state.passwords,
    )
    secret, user = await svc.login(data, limiter=request.app.state.login_limiter)
    response.set_cookie(
        cookie_name(runtime.settings),
        secret,
        path="/",
        httponly=True,
        secure=runtime.settings.env == "production",
        samesite="lax",
        max_age=runtime.settings.session_hours * 3600,
    )
    return {"user": user, "csrf_token": csrf_token(secret)}


@router.get("/auth/me")
async def me(request: Request):
    svc = _service(request)
    result = await svc.me()
    result["csrf_token"] = (
        csrf_token(svc.context.credential.secret)
        if svc.context.credential.kind == "session"
        else None
    )
    return result


@router.post("/auth/logout", status_code=204)
async def logout(request: Request, response: Response):
    await _service(request).logout()
    clear_cookie(request, response)


@router.post("/auth/password", status_code=204)
async def change_password(data: schemas.PasswordChange, request: Request, response: Response):
    await _service(request).change_password(data)
    clear_cookie(request, response)

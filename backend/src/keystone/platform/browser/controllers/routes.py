import asyncio
from uuid import UUID, uuid4

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect

from keystone.core.errors import ServiceError
from keystone.core.http import Page
from keystone.core.time import utcnow
from keystone.platform.browser.adapters.opensandbox import RuntimeUnavailable
from keystone.platform.browser.services.browser import BrowserService
from keystone.platform.identity.controllers.dependencies import (
    access_context,
    socket_access_context,
)

router = APIRouter(prefix="/api/v1/browser-workspaces", tags=["browser"])


def _service(connection, context):
    return BrowserService(
        context,
        connection.app.state.browser_runtime,
        connection.app.state.browser_controls,
        connection.app.state.runtime.managed_files(context),
    )


@router.get("")
async def workspaces(request: Request, page: Page):
    return await _service(request, access_context(request)).listing(page.offset, page.limit)


@router.post("/{workspace_id}/open")
async def open_browser(request: Request, workspace_id: UUID):
    return await _service(request, access_context(request)).open(workspace_id)


@router.post("/{workspace_id}/backup")
async def backup_browser(request: Request, workspace_id: UUID):
    return await _service(request, access_context(request)).backup(workspace_id)


@router.post("/{workspace_id}/runs/{run_id}/stop")
async def stop_browser(request: Request, workspace_id: UUID, run_id: UUID):
    return await _service(request, access_context(request)).stop(workspace_id, run_id)


@router.post("/{workspace_id}/runs/{run_id}/check")
async def check_browser(request: Request, workspace_id: UUID, run_id: UUID):
    return await _service(request, access_context(request)).check(workspace_id, run_id)


async def _relay(socket, upstream, control, verify, expiry):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + min(5, (expiry - utcnow()).total_seconds())

    def allowed():
        return not control.stop.is_set() and loop.time() < deadline

    async def input_frames():
        while allowed():
            frame = await socket.receive_bytes()
            if len(frame) > 1024 * 1024 or not allowed():
                return
            await upstream.send(frame)

    async def output_frames():
        async for frame in upstream:
            if not allowed() or not isinstance(frame, bytes):
                return
            await socket.send_bytes(frame)

    async def reauthorize():
        nonlocal deadline
        while allowed():
            await asyncio.sleep(min(2, max(0, deadline - loop.time())))
            if not allowed():
                return
            async with asyncio.timeout_at(deadline):
                _, expiry = await verify()
            deadline = loop.time() + min(5, (expiry - utcnow()).total_seconds())

    tasks = [
        asyncio.create_task(task())
        for task in (input_frames, output_frames, reauthorize, control.stop.wait)
    ]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@router.websocket("/{workspace_id}/runs/{run_id}/screen")
async def screen(socket: WebSocket, workspace_id: UUID, run_id: UUID):
    socket.state.request_id = uuid4().hex
    code = 1000
    accepted = False
    try:
        service = _service(socket, socket_access_context(socket))
        async with service.control(workspace_id, run_id) as (control, sandbox_id, _):
            async with service.runtime.stream(sandbox_id, run_id) as upstream:

                async def verify():
                    return await service.stream_authorized(workspace_id, run_id)

                async with asyncio.timeout(3):
                    _, expiry = await verify()
                if control.stop.is_set():
                    return
                await socket.accept(
                    subprotocol="binary" if "binary" in socket.scope["subprotocols"] else None
                )
                accepted = True
                await _relay(socket, upstream, control, verify, expiry)
    except ServiceError:
        code = 1008
    except (RuntimeUnavailable, TimeoutError, KeyError):
        code = 1011
    except WebSocketDisconnect:
        return
    finally:
        try:
            await socket.close(code=code if accepted else 1008)
        except (RuntimeError, WebSocketDisconnect):
            pass

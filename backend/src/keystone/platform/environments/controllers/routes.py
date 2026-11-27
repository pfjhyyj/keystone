import asyncio
from uuid import UUID, uuid4

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect

from keystone.core.errors import ServiceError
from keystone.core.http import ListTotal, Page
from keystone.core.time import utcnow
from keystone.platform.environments.adapters.opensandbox import RuntimeUnavailable
from keystone.platform.environments.schemas.environments import EnvironmentRequest
from keystone.platform.environments.services.environments import EnvironmentService
from keystone.platform.identity.controllers.dependencies import (
    access_context,
    socket_access_context,
)

router = APIRouter(prefix="/api/v1/environments", tags=["environments"])


def _service(connection, context):
    return EnvironmentService(
        context,
        connection.app.state.opensandbox,
        connection.app.state.runtime.managed_files(context),
        connection.app.state.runtime.environment_keys,
    )


@router.get("")
async def environments(request: Request, page: Page, total: ListTotal):
    return await _service(request, access_context(request)).listing(
        page.offset, page.limit, total=total
    )


@router.post("")
async def ensure_environment(request: Request, data: EnvironmentRequest):
    return await _service(request, access_context(request)).ensure(data.kind, data.key)


@router.post("/{environment_id}/open")
async def open_environment(request: Request, environment_id: UUID):
    return await _service(request, access_context(request)).open(environment_id)


@router.post("/{environment_id}/backup")
async def backup_environment(request: Request, environment_id: UUID):
    return await _service(request, access_context(request)).backup(environment_id)


@router.post("/{environment_id}/sessions/{session_id}/stop")
async def stop_session(request: Request, environment_id: UUID, session_id: UUID):
    return await _service(request, access_context(request)).stop(environment_id, session_id)


@router.post("/{environment_id}/sessions/{session_id}/check")
async def check_session(request: Request, environment_id: UUID, session_id: UUID):
    return await _service(request, access_context(request)).check(environment_id, session_id)


async def _relay(socket, upstream, verify, expiry):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + min(5, (expiry - utcnow()).total_seconds())

    def allowed():
        # Renewal every 2s rechecks the hold; a stop or lost hold ends forwarding within 5s.
        return loop.time() < deadline

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

    tasks = [asyncio.create_task(task()) for task in (input_frames, output_frames, reauthorize)]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@router.websocket("/{environment_id}/sessions/{session_id}/screen")
async def screen(socket: WebSocket, environment_id: UUID, session_id: UUID):
    socket.state.request_id = uuid4().hex
    code = 1000
    accepted = False
    try:
        service = _service(socket, socket_access_context(socket))
        async with service.control(environment_id, session_id) as (control, sandbox_id, _):
            async with service.runtime.stream(sandbox_id, session_id) as upstream:

                async def verify():
                    return await service.renew(environment_id, session_id, control)

                async with asyncio.timeout(3):
                    _, expiry = await verify()
                await socket.accept(
                    subprotocol="binary" if "binary" in socket.scope["subprotocols"] else None
                )
                accepted = True
                await _relay(socket, upstream, verify, expiry)
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

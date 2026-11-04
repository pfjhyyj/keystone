"""HTTP reads queue within capacity and recheck access before sending bytes."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, asynccontextmanager, contextmanager
from threading import Event
from uuid import UUID

import pytest
from fastapi import Request
from sqlalchemy import text
from test_files_dictionary import client as client
from test_files_dictionary import upload
from test_identity import query

from keystone.core.content_response import ContentResponse
from keystone.core.errors import ServiceError
from keystone.platform.files.services.files import FileService
from keystone.platform.identity.controllers.dependencies import access_context

pytestmark = pytest.mark.postgres


def watch_wait(storage, monkeypatch):
    entered = Event()
    original = storage.transfers.slot

    @contextmanager
    def tracked():
        try:
            with original() as number:
                yield number
        except ServiceError as error:
            if error.code == "file_transfer_busy":
                entered.set()
            raise

    monkeypatch.setattr(storage.transfers, "slot", tracked)
    return original, entered


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize(
    "change,status", [(None, 200), ("revoke", 404), ("disable", 409), ("delete", 409)]
)
def test_http_read_waits_for_capacity_and_rechecks_access(
    client, monkeypatch, method, change, status
):
    file = upload(client, b"0123456789", content_type="video/mp4").json()
    storage = client.app.state.runtime.storage
    slot, entered = watch_wait(storage, monkeypatch)
    with ThreadPoolExecutor(1) as pool:
        with ExitStack() as held:
            for _ in range(storage.transfers.settings.file_transfer_slots):
                held.enter_context(slot())
            pending = pool.submit(
                client.request,
                method,
                f"/api/v1/_test/materials/{file['id']}/content",
                headers={"Range": "bytes=2-5"},
            )
            assert entered.wait(5)
            assert not pending.done()
            if change == "revoke":
                query(
                    client,
                    lambda session: session.execute(
                        text("DELETE FROM keystone.test_materials WHERE id=:id"),
                        {"id": UUID(file["id"])},
                    ),
                )
            elif change == "disable":
                assert (
                    client.patch(
                        f"/api/v1/_test/materials/{file['id']}",
                        json={"name": file["name"], "status": "disabled"},
                    ).status_code
                    == 200
                )
            elif change == "delete":
                assert client.delete(f"/api/v1/_test/materials/{file['id']}").status_code == 202
        response = pending.result(timeout=5)
    if change:
        assert response.status_code == status
    elif method == "GET":
        assert response.status_code == 206 and response.content == b"2345"
        assert response.headers["content-range"] == "bytes 2-5/10"
    else:
        assert response.status_code == 200 and response.content == b""
        assert response.headers["content-length"] == "10"


def test_disconnect_while_waiting_does_not_send_headers_or_leak_slots(client, monkeypatch):
    identifier = UUID(upload(client).json()["id"])
    runtime = client.app.state.runtime
    slot, entered = watch_wait(runtime.storage, monkeypatch)

    @client.app.get("/_acceptance/queued-disconnect")
    async def disconnect(request: Request):
        async def receive():
            assert await asyncio.to_thread(entered.wait, 5)
            return {"type": "http.disconnect"}

        async def send(message):
            pytest.fail("Headers sent after disconnect")

        service = FileService(access_context(request), runtime.storage)

        @asynccontextmanager
        async def read():
            async with (
                service.read_transfer() as transfer,
                transfer.open_content(identifier) as reader,
            ):
                yield reader

        await ContentResponse(read(), request)(request.scope, receive, send)
        return {"closed": True}

    with ExitStack() as held:
        for _ in range(runtime.settings.file_transfer_slots):
            held.enter_context(slot())
        assert client.get("/_acceptance/queued-disconnect").json() == {"closed": True}
    with ExitStack() as held:
        for _ in range(runtime.settings.file_transfer_slots):
            held.enter_context(slot())


def test_http_read_returns_busy_after_bounded_wait_without_leaking_slots(client):
    file = upload(client).json()
    transfers = client.app.state.runtime.storage.transfers
    with ExitStack() as held:
        for _ in range(transfers.settings.file_transfer_slots):
            held.enter_context(transfers.slot())
        response = client.get(f"/api/v1/_test/materials/{file['id']}/content")
        assert response.status_code == 503
        assert response.json()["code"] == "file_transfer_busy"
    with ExitStack() as held:
        for _ in range(transfers.settings.file_transfer_slots):
            held.enter_context(transfers.slot())

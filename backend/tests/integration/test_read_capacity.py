"""Content reads have no global file-count gate and still authorize each request."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import AsyncExitStack
from threading import Event
from uuid import UUID

import pytest
from test_files_dictionary import client as client
from test_files_dictionary import upload

pytestmark = pytest.mark.postgres


def test_eight_active_workspaces_do_not_block_range_or_head(client):
    file = upload(client, b"0123456789", content_type="video/mp4").json()
    runtime = client.app.state.runtime
    entered, release = Event(), Event()

    async def hold():
        async with AsyncExitStack() as held:
            for _ in range(8):
                await held.enter_async_context(runtime.storage.transfers.temporary(1))
            entered.set()
            assert await asyncio.to_thread(release.wait, 10)

    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(client.portal.call, hold)
        try:
            assert entered.wait(5)
            path = f"/api/v1/_test/materials/{file['id']}/content"
            result = client.get(path, headers={"Range": "bytes=2-5"})
            assert result.status_code == 206 and result.content == b"2345"
            assert client.head(path).headers["content-length"] == "10"
        finally:
            release.set()
        pending.result(timeout=5)
    assert not list((runtime.settings.file_coordination_root / ".transfers").iterdir())


def test_each_new_read_rechecks_the_business_relationship(client):
    from sqlalchemy import text
    from test_identity import query

    file = upload(client).json()
    path = f"/api/v1/_test/materials/{file['id']}/content"
    assert client.get(path).status_code == 200
    query(
        client,
        lambda session: session.execute(
            text("DELETE FROM keystone.test_materials WHERE id=:id"), {"id": UUID(file["id"])}
        ),
    )
    assert client.get(path).status_code == 404

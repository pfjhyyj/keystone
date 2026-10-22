"""Upload failure facts settle before leases are released after cancellation."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from keystone.platform.files.services.files import FileService


async def test_repeated_cancellation_waits_for_failure_transaction(monkeypatch):
    entered, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    identifier = uuid4()
    row = SimpleNamespace(status="uploading")

    @asynccontextmanager
    async def transaction():
        try:
            yield None
        finally:
            closed.set()

    async def locked(session, requested):
        assert requested == identifier
        entered.set()
        await release.wait()
        return row

    audit = AsyncMock()
    monkeypatch.setattr("keystone.platform.files.services.access.locked", locked)
    monkeypatch.setattr("keystone.platform.files.services.files.audit.record", audit)
    identity = SimpleNamespace(
        database=SimpleNamespace(transaction=transaction), correlation_id="failure-request"
    )
    service = FileService(identity, None)
    task = asyncio.create_task(service._fail_upload(identifier))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        for _ in range(2):
            task.cancel()
            await asyncio.sleep(0)
        assert not task.done(), "Cancellation must wait for the known cleanup transaction"
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert closed.is_set()
    assert row.status == "failed"
    audit.assert_awaited_once()
    assert audit.await_args.args[1] == "file.upload.failure"
    assert audit.await_args.kwargs["correlation_id"] == "failure-request"

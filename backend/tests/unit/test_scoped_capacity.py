"""Run-scoped byte promises retain OS ownership and global disk protections."""

import asyncio
import json

import pytest
from test_disk_capacity import child, clean, registry

from keystone.core.errors import ServiceError
from keystone.platform.files.schemas.transfers import TransferScope


def scoped(storage, size=4, owner="flow:test"):
    return storage.transfers.scoped(TransferScope(owner=owner, max_temporary_bytes=size))


async def test_flushed_scratch_still_consumes_scope_until_workspace_closes(tmp_path):
    storage = registry(tmp_path)
    transfers = scoped(storage)
    async with transfers.workspace(4) as workspace:
        with workspace.path.open("wb") as stream:
            workspace.capacity.write(stream, b"done")
        assert sum(workspace.capacity.amounts.values()) == 0
        with pytest.raises(ServiceError) as error:
            async with transfers.workspace(1):
                pytest.fail("Flushed bytes must not release the Run's promise")
        assert error.value.code == "file_scope_limit"
        async with scoped(storage, owner="flow:other").workspace(1):
            pass
        async with storage.transfers.workspace(1):
            pass
        assert storage.transfers.scope is None
    async with transfers.workspace(4):
        pass
    clean(tmp_path)


async def test_live_process_scope_and_stricter_limit_cannot_be_bypassed(tmp_path):
    storage = registry(tmp_path)
    body = """from keystone.platform.files.schemas.transfers import TransferScope
async def run():
    transfer = storage.transfers.scoped(TransferScope(owner='flow:test', max_temporary_bytes=4))
    try:
        async with transfer.workspace(1):
            print('accepted')
    except ServiceError as error:
        print(error.code)
asyncio.run(run())
"""
    async with scoped(storage).workspace(4):
        result = await asyncio.to_thread(child, tmp_path, body)
        assert result.returncode == 0 and result.stdout.strip() == "file_scope_limit", result.stderr
        with pytest.raises(ServiceError):
            async with scoped(storage, size=100).workspace(1):
                pytest.fail("A looser process declaration bypassed the live owner's stricter scope")
    result = await asyncio.to_thread(child, tmp_path, body)
    assert result.returncode == 0 and result.stdout.strip() == "accepted", result.stderr
    clean(tmp_path)


async def test_dead_scoped_process_releases_only_its_own_promise_and_scratch(tmp_path):
    storage = registry(tmp_path)
    body = """from keystone.platform.files.schemas.transfers import TransferScope
async def run():
    transfer = storage.transfers.scoped(TransferScope(owner='flow:test', max_temporary_bytes=4))
    async with transfer.workspace(4) as workspace:
        workspace.path.write_bytes(b'dead')
        os._exit(71)
asyncio.run(run())
"""
    result = await asyncio.to_thread(child, tmp_path, body)
    assert result.returncode == 71, result.stderr
    async with scoped(storage).workspace(4) as workspace:
        assert not workspace.path.exists()
        workspace.path.write_bytes(b"live")
        result = await asyncio.to_thread(
            child, tmp_path, body.replace("workspace(4)", "workspace(1)")
        )
        assert result.returncode != 71, "A live scope promise must not be reclaimed"
        assert workspace.path.read_bytes() == b"live"
    clean(tmp_path)


async def test_corrupt_live_scoped_metadata_fails_closed(tmp_path):
    storage = registry(tmp_path)
    transfers = scoped(storage)
    async with transfers.workspace(2) as workspace:
        path = workspace.capacity.capacity.directory() / f"{workspace.capacity.identifier}.json"
        original = path.read_text()
        record = json.loads(original)
        assert record["version"] == 2 and record["scope_bytes"] == 2
        record["scope_bytes"] = -1
        try:
            path.write_text(json.dumps(record))
            with pytest.raises(ServiceError) as error:
                async with transfers.workspace(1):
                    pytest.fail("Corrupt live promise must not authorize writes")
            assert error.value.code == "file_capacity_unavailable"
        finally:
            path.write_text(original)
    clean(tmp_path)

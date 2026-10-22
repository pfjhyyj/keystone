import asyncio
import hashlib
import io
import os
import subprocess
import sys
import tracemalloc
from contextlib import ExitStack
from threading import Event

import pytest

from keystone.core.config import Settings
from keystone.core.errors import ServiceError
from keystone.platform.files.adapters.storage import StorageRegistry, blocking, opened
from keystone.platform.files.adapters.transfers import CHUNK_BYTES, checksum


def registry(tmp_path, **settings):
    value = StorageRegistry(
        Settings(
            config_file=None,
            file_coordination_root=tmp_path,
            file_storages={"local": {"driver": "filesystem", "root": tmp_path}},
            **settings,
        )
    )
    value.initialize()
    return value


async def test_large_stream_has_bounded_python_memory(tmp_path):
    size = 128 * 1024**2
    storage = registry(tmp_path, file_max_bytes=size)
    chunk = b"x" * CHUNK_BYTES

    async def chunks():
        for _ in range(size // len(chunk)):
            yield chunk

    tracemalloc.start()
    try:
        async with storage.transfers.stage(chunks(), size) as staged:
            assert staged.path.stat().st_size == size
            with staged.path.open("rb") as source:
                assert staged.checksum == hashlib.file_digest(source, "sha256").hexdigest()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 8 * 1024**2, peak
    assert not list((tmp_path / ".transfers").iterdir())


async def test_capacity_quota_disconnect_checksum_and_reuse(tmp_path):
    storage = registry(tmp_path, file_transfer_slots=1, file_max_bytes=10, file_temp_bytes=5)
    other = StorageRegistry(storage.transfers.settings)
    with storage.transfers.slot():
        with pytest.raises(ServiceError, match="已满"):
            with other.transfers.slot():
                pytest.fail("Slot was acquired twice")
    with pytest.raises(ServiceError) as rejected:
        async with storage.transfers.temporary(6):
            pytest.fail("Quota was ignored")
    assert rejected.value.code == "file_temporary_limit"

    async def disconnected():
        yield b"hi"
        raise ConnectionError("disconnected")

    with pytest.raises(ConnectionError):
        async with storage.transfers.stage(disconnected(), 3):
            pytest.fail("Interrupted content was accepted")
    assert not list((tmp_path / ".transfers").iterdir())

    async def chunks():
        yield b"hi"

    for size, expected_checksum, code in [
        (3, None, "file_size_mismatch"),
        (1, None, "file_too_large"),
        (2, "0" * 64, "file_checksum_mismatch"),
    ]:
        with pytest.raises(ServiceError) as rejected:
            async with storage.transfers.stage(chunks(), size, expected_checksum):
                pytest.fail("Invalid content accepted")
        assert rejected.value.code == code
    async with storage.transfers.stage(chunks(), 2) as staged:
        assert staged.path.read_bytes() == b"hi"


async def test_cancel_releases_scratch_and_slot(tmp_path):
    storage = registry(tmp_path)
    entered = asyncio.Event()

    async def chunks():
        yield b"x"
        entered.set()
        await asyncio.Event().wait()

    async def upload():
        async with storage.transfers.stage(chunks(), 10):
            pytest.fail("Cancelled upload accepted")

    task = asyncio.create_task(upload())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not list((tmp_path / ".transfers").iterdir())
    with ExitStack() as stack:
        for _ in range(storage.transfers.settings.file_transfer_slots):
            stack.enter_context(storage.transfers.slot())


async def test_read_slot_wait_is_bounded_and_cancellable(tmp_path):
    storage = registry(tmp_path, file_transfer_slots=1)
    with storage.transfers.slot():
        with pytest.raises(ServiceError) as busy:
            async with storage.transfers.read_slot(timeout=0.05):
                pytest.fail("Read exceeded the shared capacity")
        assert busy.value.code == "file_transfer_busy"
        entered = asyncio.Event()

        async def waiting():
            entered.set()
            async with storage.transfers.read_slot(timeout=3):
                pytest.fail("Cancelled reader acquired a slot")

        task = asyncio.create_task(waiting())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    with storage.transfers.slot():
        pass


async def test_cancel_during_open_waits_and_closes_handle(tmp_path):
    entered, release = Event(), Event()
    handles = []

    def acquire():
        handle = (tmp_path / "test").open("wb")
        handles.append(handle)
        entered.set()
        release.wait(5)
        return handle

    async def operation():
        async with opened(acquire):
            pytest.fail("Cancelled acquisition continued")

    task = asyncio.create_task(operation())
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert handles[0].closed


async def test_process_crash_residue_reclaimed_under_slot_lock(tmp_path):
    storage = registry(tmp_path, file_transfer_slots=1)
    code = f"""
import os
from pathlib import Path
from keystone.core.asyncio import run
from keystone.core.config import Settings
from keystone.platform.files.adapters.storage import StorageRegistry
async def main():
    settings = Settings(config_file=None, file_coordination_root=Path({str(tmp_path)!r}),
        file_transfer_slots=1)
    storage = StorageRegistry(settings)
    async with storage.transfers.temporary(10) as path:
        path.write_bytes(b'partial')
        os._exit(71)
run(main())
"""
    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", code],
        capture_output=True,
        timeout=20,
        env={**os.environ, "KEYSTONE_CONFIG_FILE": ""},
    )
    assert result.returncode == 71, result.stderr
    async with storage.transfers.temporary(10) as path:
        assert not path.exists()
    assert not list((tmp_path / ".transfers").iterdir())


async def test_growing_file_hash_is_bounded_and_cancellable():
    class GrowingFile:
        calls = 0

        def read(self, size):
            self.calls += 1
            assert self.calls <= 3, "Unbounded scan of a growing file"
            return b"x" * size

    growing = GrowingFile()
    with pytest.raises(ValueError, match="size changed"):
        checksum(growing, CHUNK_BYTES * 2)
    assert growing.calls == 3

    entered, release, cancelled = Event(), Event(), Event()

    class PausedFile(io.BytesIO):
        def read(self, size):
            entered.set()
            assert release.wait(5)
            return super().read(size)

    source = PausedFile(b"x" * CHUNK_BYTES * 2)
    task = asyncio.create_task(
        blocking(checksum, source, CHUNK_BYTES * 2, cancelled, on_cancel=cancelled.set)
    )
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert source.tell() == CHUNK_BYTES


async def test_cancel_final_copy_stops_before_writing_all_content(tmp_path, monkeypatch):
    storage = registry(tmp_path)
    source = storage.get()
    key = "files/" + "a" * 32
    entered, release = Event(), Event()
    from pathlib import Path

    original = Path.open

    class PausedFile:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def read(self, size):
            entered.set()
            assert release.wait(5)
            return self.stream.read(size)

    async def chunks():
        for _ in range(4):
            yield b"x" * CHUNK_BYTES

    async with storage.transfers.stage(chunks(), CHUNK_BYTES * 4) as staged:

        def open_source(path, *args, **kwargs):
            stream = original(path, *args, **kwargs)
            return PausedFile(stream) if path == staged.path and args == ("rb",) else stream

        monkeypatch.setattr(Path, "open", open_source)
        task = asyncio.create_task(
            blocking(
                source.write, source.fingerprint(), key, staged, on_cancel=staged.cancelled.set
            )
        )
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (source.root / key).stat().st_size < staged.size

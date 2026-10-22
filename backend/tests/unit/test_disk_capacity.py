import asyncio
import errno
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Queue
from threading import Barrier, Event
from types import SimpleNamespace

import pytest

from keystone.core.config import Settings
from keystone.core.errors import ServiceError
from keystone.platform.files.adapters import capacity as disk
from keystone.platform.files.adapters.storage import StorageRegistry


def registry(root, **overrides):
    settings = Settings(
        config_file=None,
        file_coordination_root=root,
        file_storages={"local": {"driver": "filesystem", "root": root}},
        **{"file_min_free_bytes": 1, **overrides},
    )
    value = StorageRegistry(settings)
    value.initialize()
    return value


def free_space(monkeypatch, free):
    state = SimpleNamespace(free=free)
    monkeypatch.setattr(disk.shutil, "disk_usage", lambda path: state)
    return state


def clean(root):
    assert not list((root / ".capacity").iterdir())
    if (root / ".transfers").exists():
        assert not list((root / ".transfers").iterdir())


async def test_upload_reserves_both_copies_before_consuming_body(tmp_path, monkeypatch):
    storage = registry(tmp_path)
    free_space(monkeypatch, 4)  # Two bytes scratch + two final + one byte floor need five.
    consumed = False

    async def chunks():
        nonlocal consumed
        consumed = True
        yield b"hi"

    with pytest.raises(ServiceError) as rejected:
        async with storage.transfers.stage(chunks(), 2, destination=tmp_path / "files"):
            pytest.fail("Insufficient two-copy capacity was admitted")
    assert rejected.value.status == 507
    assert not consumed
    clean(tmp_path)


@pytest.mark.parametrize(
    "temporary_free,final_free,allowed", [(10, 2, False), (2, 10, False), (3, 3, True)]
)
async def test_temp_and_destination_filesystems_are_checked_independently(
    tmp_path, monkeypatch, temporary_free, final_free, allowed
):
    storage = registry(tmp_path)
    target = tmp_path / "files"
    monkeypatch.setattr(disk, "filesystem", lambda path: "2" if Path(path) == target else "1")
    monkeypatch.setattr(
        disk.shutil,
        "disk_usage",
        lambda path: SimpleNamespace(free=final_free if Path(path) == target else temporary_free),
    )

    async def enter():
        async with storage.transfers.workspace(2, destination=target) as workspace:
            assert workspace.capacity.amounts == {"1": 2, "2": 2}

    if allowed:
        await enter()
    else:
        with pytest.raises(ServiceError) as rejected:
            await enter()
        assert rejected.value.code == "file_capacity_full"
    clean(tmp_path)


async def test_coordination_scratch_and_destination_can_use_three_filesystems(
    tmp_path, monkeypatch
):
    storage = registry(tmp_path)
    devices = {tmp_path: "1", tmp_path / ".transfers": "2", tmp_path / "files": "3"}
    monkeypatch.setattr(disk, "filesystem", lambda path: devices[Path(path)])
    free_space(monkeypatch, 100)
    async with storage.transfers.workspace(2, destination=tmp_path / "files") as workspace:
        assert workspace.capacity.amounts == {"1": 0, "2": 2, "3": 2}
        workspace.capacity.check(workspace.path, 2)
    clean(tmp_path)


def test_concurrent_registries_cannot_spend_the_same_free_space(tmp_path, monkeypatch):
    stores = [registry(tmp_path), registry(tmp_path)]
    free_space(monkeypatch, 5)
    barrier, release, results = Barrier(2), Event(), Queue()

    def attempt(storage):
        barrier.wait(5)
        try:
            allocation = storage.transfers.capacity.reserve([(tmp_path / "files", 3)])
        except ServiceError as error:
            results.put(error.code)
        else:
            try:
                results.put("accepted")
                assert release.wait(5)
            finally:
                allocation.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(attempt, store) for store in stores]
        try:
            assert sorted([results.get(timeout=5), results.get(timeout=5)]) == [
                "accepted",
                "file_capacity_full",
            ]
        finally:
            release.set()
        for future in futures:
            future.result(timeout=5)
    clean(tmp_path)


def child(root, body):
    code = f"""
import asyncio, os
from pathlib import Path
from types import SimpleNamespace
from keystone.core.config import Settings
from keystone.core.errors import ServiceError
from keystone.platform.files.adapters import capacity as disk
from keystone.platform.files.adapters.storage import StorageRegistry
root = Path({str(root)!r})
settings = Settings(config_file=None, file_coordination_root=root,
    file_storages={{"local": {{"driver": "filesystem", "root": root}}}}, file_min_free_bytes=1)
storage = StorageRegistry(settings)
{body}
"""
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=20,
        env={**os.environ, "KEYSTONE_CONFIG_FILE": ""},
    )


def test_live_process_reservations_are_not_reclaimed_by_another_process(tmp_path, monkeypatch):
    storage = registry(tmp_path)
    free_space(monkeypatch, 5)
    body = """
disk.shutil.disk_usage = lambda path: SimpleNamespace(free=5)
try:
    with storage.transfers.capacity.reserve([(root / 'files', 3)]):
        print('accepted')
except ServiceError as error:
    print(error.code)
"""
    with storage.transfers.capacity.reserve([(tmp_path / "files", 3)]):
        result = child(tmp_path, body)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "file_capacity_full"
    result = child(tmp_path, body)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "accepted"
    clean(tmp_path)


async def test_crash_recovery_reclaims_only_dead_scratch_and_preserves_live_owner(tmp_path):
    storage = registry(tmp_path)
    body = """
async def run():
    async with storage.transfers.temporary(10) as path:
        path.write_bytes(b'dead')
        os._exit(71)
asyncio.run(run())
"""
    async with storage.transfers.workspace(10) as live:
        live.path.write_bytes(b"live")
        identifier = live.capacity.identifier
        result = await asyncio.to_thread(child, tmp_path, body)
        assert result.returncode == 71, result.stderr
        # The live workspace's monitor may already have reclaimed the dead owner.
        # Assert the eventual state, without requiring a particular scheduling order.
        with storage.transfers.capacity.reserve([(tmp_path / "files", 0)]):
            assert live.path.read_bytes() == b"live"
            assert not (tmp_path / ".transfers/1").exists()
            assert (tmp_path / f".capacity/{identifier}.json").is_file()
        assert live.path.read_bytes() == b"live"
    clean(tmp_path)


def test_partial_dead_record_is_reclaimed_but_corrupt_live_record_fails_closed(tmp_path):
    storage = registry(tmp_path)
    capacity = storage.transfers.capacity
    directory = capacity.directory()
    identifier = "a" * 32
    (directory / f"{identifier}.lease").write_bytes(b"")
    (directory / f"{identifier}.tmp").write_bytes(b"partial")
    note = directory / "unrelated-note"
    note.write_text("keep", encoding="utf-8")
    with capacity.reserve([(tmp_path / "files", 0)]) as live:
        assert not (directory / f"{identifier}.lease").exists()
        assert not (directory / f"{identifier}.tmp").exists()
        assert note.read_text(encoding="utf-8") == "keep"
        (directory / f"{live.identifier}.json").write_bytes(b"{")
        with pytest.raises(ServiceError) as rejected:
            capacity.reserve([(tmp_path / "files", 0)])
        assert rejected.value.code == "file_capacity_unavailable"
        assert not live.closed
    note.unlink()
    clean(tmp_path)


def test_flushed_bytes_consume_reservation_without_double_charging(tmp_path, monkeypatch):
    storage = registry(tmp_path)
    state = free_space(monkeypatch, 5)

    class Writer:
        def __init__(self, stream):
            self.stream = stream

        def fileno(self):
            return self.stream.fileno()

        def write(self, data):
            written = self.stream.write(data)
            state.free -= written
            return written

        def flush(self):
            self.stream.flush()

    capacity = storage.transfers.capacity
    with capacity.reserve([(tmp_path / "files", 4)]) as allocation:
        with (tmp_path / "files/test").open("wb") as stream:
            writer = Writer(stream)
            allocation.write(writer, b"ab")
            assert list(allocation.amounts.values()) == [2]
            allocation.write(writer, b"cd")
            assert list(allocation.amounts.values()) == [0]
        assert state.free == 1
        with pytest.raises(ServiceError) as rejected:
            capacity.reserve([(tmp_path / "files", 1)])
        assert rejected.value.code == "file_capacity_full"
    clean(tmp_path)


async def test_space_loss_during_upload_stops_writing_and_releases_everything(
    tmp_path, monkeypatch
):
    storage = registry(tmp_path)
    state = free_space(monkeypatch, 10)

    async def chunks():
        yield b"a"
        records = list((tmp_path / ".capacity").glob("*.json"))
        assert len(records) == 1
        assert list(json.loads(records[0].read_text())["amounts"].values()) == [3]
        state.free = 1
        yield b"b"

    with pytest.raises(ServiceError) as rejected:
        async with storage.transfers.stage(chunks(), 2, destination=tmp_path / "files"):
            pytest.fail("Low-space upload continued")
    assert rejected.value.code == "file_capacity_full"
    clean(tmp_path)


@pytest.mark.parametrize("failure", ["space", "oversize"])
async def test_external_writer_is_cancelled_and_cleaned_when_capacity_changes(
    tmp_path, monkeypatch, failure
):
    storage = registry(tmp_path)
    state = free_space(monkeypatch, 100)
    with pytest.raises(ServiceError) as rejected:
        async with storage.transfers.temporary(10) as path:
            path.write_bytes(b"x" * (11 if failure == "oversize" else 1))
            if failure == "space":
                state.free = 0
            await asyncio.sleep(2)
            pytest.fail("External writer was not stopped")
    assert rejected.value.code == (
        "file_capacity_full" if failure == "space" else "file_temporary_limit"
    )
    clean(tmp_path)


@pytest.mark.parametrize("failure", ["space", "oversize"])
async def test_fast_external_writer_is_checked_before_workspace_exit(
    tmp_path, monkeypatch, failure
):
    storage = registry(tmp_path)
    state = free_space(monkeypatch, 100)
    with pytest.raises(ServiceError) as rejected:
        async with storage.transfers.temporary(10) as path:
            path.write_bytes(b"x" * (11 if failure == "oversize" else 1))
            if failure == "space":
                state.free = 0
            # No await: finish before the periodic monitor can run.
    assert rejected.value.code == (
        "file_capacity_full" if failure == "space" else "file_temporary_limit"
    )
    clean(tmp_path)


@pytest.mark.parametrize("operation", ["check", "write"])
def test_filesystem_change_after_reservation_fails_closed(tmp_path, monkeypatch, operation):
    storage = registry(tmp_path)
    capacity = storage.transfers.capacity
    target = tmp_path / "files"
    original = disk.filesystem
    with capacity.reserve([(target, 2)]) as allocation:
        monkeypatch.setattr(disk, "filesystem", lambda path: str(int(original(path)) + 1))
        with (target / "test").open("wb") as stream:
            with pytest.raises(ServiceError) as rejected:
                if operation == "write":
                    allocation.write(stream, b"ab")
                else:
                    allocation.check(target / "test", 2)
        assert rejected.value.code == "file_capacity_unavailable"
        assert (target / "test").read_bytes() == b""
    clean(tmp_path)


def test_same_filesystem_targets_are_all_revalidated(tmp_path, monkeypatch):
    storage = registry(tmp_path)
    capacity = storage.transfers.capacity
    target = tmp_path / "files"
    original = disk.filesystem
    with capacity.reserve([(tmp_path, 2), (target, 2)]) as allocation:
        monkeypatch.setattr(
            disk, "filesystem", lambda path: "0" if Path(path) == tmp_path else original(path)
        )
        with pytest.raises(ServiceError) as rejected:
            allocation.check(tmp_path / "scratch", 2)
        assert rejected.value.code == "file_capacity_unavailable"
    clean(tmp_path)


def test_cross_filesystem_writes_consume_only_their_own_disk_claim(tmp_path, monkeypatch):
    storage = registry(tmp_path)
    temporary, target = tmp_path / "work", tmp_path / "files"
    temporary.mkdir()
    temporary_device = disk.filesystem(tmp_path)
    final_device = str(int(temporary_device) + 1)
    free = {temporary_device: 3, final_device: 3}
    monkeypatch.setattr(
        disk, "filesystem", lambda path: final_device if Path(path) == target else temporary_device
    )
    monkeypatch.setattr(
        disk.shutil, "disk_usage", lambda path: SimpleNamespace(free=free[disk.filesystem(path)])
    )
    capacity = storage.transfers.capacity
    with (
        capacity.reserve([(temporary, 2), (target, 2)]) as allocation,
        (temporary / "scratch").open("wb") as scratch,
        (target / "final").open("wb") as final,
    ):
        devices = {scratch.fileno(): temporary_device, final.fileno(): final_device}
        original = os.fstat
        monkeypatch.setattr(
            disk.os,
            "fstat",
            lambda fd: SimpleNamespace(st_dev=int(devices[fd])) if fd in devices else original(fd),
        )

        class Writer:
            def __init__(self, stream):
                self.stream = stream

            def fileno(self):
                return self.stream.fileno()

            def write(self, data):
                written = self.stream.write(data)
                free[devices[self.fileno()]] -= written
                return written

            def flush(self):
                self.stream.flush()

        allocation.write(Writer(scratch), b"ab")
        assert allocation.amounts == {temporary_device: 0, final_device: 2}
        allocation.write(Writer(final), b"ab")
        assert allocation.amounts == {temporary_device: 0, final_device: 0}
        assert free == {temporary_device: 1, final_device: 1}
        for directory in (temporary, target):
            with pytest.raises(ServiceError) as rejected:
                capacity.reserve([(directory, 1)])
            assert rejected.value.code == "file_capacity_full"
    clean(tmp_path)


def test_final_disk_upload_also_protects_coordination_disk_floor(tmp_path, monkeypatch):
    storage = registry(tmp_path)
    target = tmp_path / "files"
    monkeypatch.setattr(disk, "filesystem", lambda path: "2" if Path(path) == target else "1")
    monkeypatch.setattr(
        disk.shutil,
        "disk_usage",
        lambda path: SimpleNamespace(free=100 if Path(path) == target else 0),
    )
    with pytest.raises(ServiceError) as rejected:
        with storage.transfers.capacity.reserve([(target, 1)]):
            pytest.fail("Reservation metadata spent the coordination disk's protected floor")
    assert rejected.value.code == "file_capacity_full"
    clean(tmp_path)


@pytest.mark.parametrize("record", ["missing", "corrupt"])
def test_orphan_scratch_is_reclaimed_without_readable_metadata(tmp_path, record):
    storage = registry(tmp_path)
    capacity = storage.transfers.capacity
    directory = capacity.directory()
    scratch = tmp_path / ".transfers/0"
    scratch.parent.mkdir()
    scratch.write_bytes(b"orphan")
    if record == "corrupt":
        identifier = "a" * 32
        (directory / f"{identifier}.lease").write_bytes(b"")
        (directory / f"{identifier}.json").write_bytes(b"{")
    with capacity.reserve([(tmp_path / "files", 0)]):
        assert not scratch.exists()
    clean(tmp_path)


def test_scratch_with_slot_owner_but_no_record_is_preserved(tmp_path):
    storage = registry(tmp_path)
    capacity = storage.transfers.capacity
    scratch = tmp_path / ".transfers/0"
    scratch.parent.mkdir()
    with storage.transfers.slot() as number:
        assert number == 0
        scratch.write_bytes(b"new owner before claim publication")
        with capacity.reserve([(tmp_path / "files", 0)]):
            assert scratch.read_bytes() == b"new owner before claim publication"
    with capacity.reserve([(tmp_path / "files", 0)]):
        assert not scratch.exists()
    clean(tmp_path)


def test_memory_upload_reserves_destination_before_truncating_existing_file(tmp_path, monkeypatch):
    storage = registry(tmp_path)
    state = free_space(monkeypatch, 100)
    source = storage.get()
    fingerprint, key = source.fingerprint(), "files/" + "a" * 32
    source.write(fingerprint, key, b"old")
    state.free = 0
    with pytest.raises(ServiceError) as rejected:
        source.write(fingerprint, key, b"new")
    assert rejected.value.code == "file_capacity_full"
    assert (tmp_path / key).read_bytes() == b"old"
    clean(tmp_path)


def test_failed_metadata_publication_releases_owner_and_claim(tmp_path, monkeypatch):
    storage = registry(tmp_path)

    def fail(self):
        raise OSError("injected metadata failure")

    monkeypatch.setattr(disk.Reservation, "save", fail)
    with pytest.raises(ServiceError) as rejected:
        storage.transfers.capacity.reserve([(tmp_path / "files", 1)])
    assert rejected.value.code == "file_capacity_unavailable"
    clean(tmp_path)


def test_higher_floor_of_another_live_process_is_respected(tmp_path, monkeypatch):
    first = registry(tmp_path, file_min_free_bytes=3)
    second = StorageRegistry(first.transfers.settings.model_copy(update={"file_min_free_bytes": 0}))
    free_space(monkeypatch, 8)
    with first.transfers.capacity.reserve([(tmp_path / "files", 3)]):
        with pytest.raises(ServiceError) as rejected:
            second.transfers.capacity.reserve([(tmp_path / "files", 3)])
        assert rejected.value.code == "file_capacity_full"
    clean(tmp_path)


async def test_cancellation_during_reservation_publication_releases_owner(tmp_path, monkeypatch):
    storage = registry(tmp_path)
    capacity = storage.transfers.capacity
    original = capacity.reserve
    entered, release = Event(), Event()

    def delayed(*args, **kwargs):
        reservation = original(*args, **kwargs)
        entered.set()
        assert release.wait(5)
        return reservation

    monkeypatch.setattr(capacity, "reserve", delayed)

    async def operation():
        async with storage.transfers.temporary(10):
            pytest.fail("Cancelled admission continued")

    task = asyncio.create_task(operation())
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    clean(tmp_path)


@pytest.mark.parametrize("number", [errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)])
def test_filesystem_full_or_quota_error_is_507_and_releases_claim(tmp_path, number):
    storage = registry(tmp_path)

    class FullWriter:
        def __init__(self, stream):
            self.stream = stream

        def fileno(self):
            return self.stream.fileno()

        def write(self, data):
            raise OSError(number, "injected full filesystem")

    with pytest.raises(ServiceError) as rejected:
        with storage.transfers.capacity.reserve([(tmp_path / "files", 1)]) as allocation:
            with (tmp_path / "files/test").open("wb") as stream:
                allocation.write(FullWriter(stream), b"a")
    assert rejected.value.status == 507
    assert rejected.value.code == "file_capacity_full"
    clean(tmp_path)


async def test_crash_after_final_write_reclaims_scratch_but_preserves_formal_content(tmp_path):
    storage = registry(tmp_path)
    key = "files/" + "b" * 32
    body = f"""
async def chunks():
    yield b'safe'
async def run():
    async with storage.transfers.stage(chunks(), 4, destination=root / 'files') as content:
        source = storage.get()
        source.write(source.fingerprint(), {key!r}, content)
        os._exit(71)
asyncio.run(run())
"""
    result = await asyncio.to_thread(child, tmp_path, body)
    assert result.returncode == 71, result.stderr
    assert (tmp_path / key).read_bytes() == b"safe"
    with storage.transfers.capacity.reserve([(tmp_path / "files", 0)]):
        assert (tmp_path / key).read_bytes() == b"safe"
        assert not list((tmp_path / ".transfers").iterdir())
    clean(tmp_path)


def test_final_file_size_is_verified_after_flush(tmp_path, monkeypatch):
    storage = registry(tmp_path)
    original = os.fsync

    def truncate(fd):
        if os.fstat(fd).st_size == 3:
            os.ftruncate(fd, 0)
        original(fd)

    monkeypatch.setattr(os, "fsync", truncate)
    source = storage.get()
    with pytest.raises(ServiceError):
        source.write(source.fingerprint(), "files/" + "c" * 32, b"abc")
    clean(tmp_path)

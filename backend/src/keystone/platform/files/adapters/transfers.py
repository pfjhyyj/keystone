"""Owned temporary directories with per-operation bounds and physical disk reservations."""

import asyncio
import hashlib
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event

from keystone.core.errors import ServiceError
from keystone.platform.files.adapters.capacity import Capacity, Reservation
from keystone.platform.files.adapters.storage import blocking, opened

CHUNK_BYTES = 256 * 1024


def checksum(stream, size, cancelled=None):
    """Hash exactly the declared size; a growing file must not keep an I/O thread alive."""
    digest, remaining = hashlib.sha256(), size
    while remaining:
        if cancelled is not None and cancelled.is_set():
            raise ValueError("File operation cancelled")
        chunk = stream.read(min(CHUNK_BYTES, remaining))
        if not chunk:
            raise ValueError("File size changed")
        remaining -= len(chunk)
        digest.update(chunk)
    if stream.read(1):
        raise ValueError("File size changed")
    return digest.hexdigest()


@dataclass(frozen=True)
class StagedUpload:
    path: Path
    size: int
    checksum: str
    cancelled: Event = field(default_factory=Event, repr=False, compare=False)
    capacity: Reservation | None = field(default=None, repr=False, compare=False)
    stream: object | None = field(default=None, repr=False, compare=False)

    def __len__(self):
        return self.size


@dataclass(frozen=True)
class Workspace:
    path: Path
    capacity: Reservation


@asynccontextmanager
async def monitor(workspace, limit):
    """Also supervise external writers; await their cancellation before removing scratch."""
    owner, failure = asyncio.current_task(), None

    async def watch():
        nonlocal failure
        while True:
            await asyncio.sleep(0.1)
            try:
                await blocking(workspace.capacity.check, workspace.path, limit)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                failure = error
                owner.cancel()
                return

    watcher = asyncio.create_task(watch())
    try:
        yield
        # Fast external writers can finish before the periodic check ever runs.
        await blocking(workspace.capacity.check, workspace.path, limit)
        if failure is not None:
            raise failure
    except asyncio.CancelledError:
        if failure is not None:
            raise failure from None
        raise
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)


class Transfers:
    def __init__(self, coordination, settings):
        self.coordination = coordination
        self.settings = settings
        self.capacity = Capacity(coordination, settings)

    @asynccontextmanager
    async def temporary(self, size):
        async with self.workspace(size) as workspace:
            yield workspace.path

    @asynccontextmanager
    async def workspace(self, size, *, destination=None):
        if type(size) is not int or not 0 <= size <= 2 * 1024**4:
            raise ValueError("Invalid workspace byte promise")
        root = self.coordination.root
        directory = root / ".transfers"

        def prepare():
            self.coordination.fingerprint()
            if (
                directory.is_symlink()
                or directory.is_junction()
                or not directory.resolve().is_relative_to(root)
            ):
                raise ServiceError(503, "storage_unavailable", "临时存储路径无效")
            directory.mkdir(exist_ok=True)

        await blocking(prepare)
        targets = [(directory, size)]
        if destination is not None:
            targets.append((destination, size))
        async with opened(lambda: self.capacity.reserve(targets, workspace=True)) as allocation:
            path = directory / allocation.identifier
            await blocking(path.mkdir)
            workspace = Workspace(path, allocation)
            async with monitor(workspace, size):
                yield workspace

    @asynccontextmanager
    async def stage(self, chunks, size, checksum=None, *, destination=None):
        async with self.workspace(size, destination=destination) as workspace:
            path, allocation = workspace.path / "upload", workspace.capacity
            actual, digest = 0, hashlib.sha256()
            async with opened(path.open, "xb") as stream:
                async for chunk in chunks:
                    actual += len(chunk)
                    if actual > size:
                        raise ServiceError(413, "file_too_large", "上传内容超过声明大小")
                    # The ASGI server owns incoming chunk allocation; we make no aggregate copy.
                    for start in range(0, len(chunk), CHUNK_BYTES):
                        part = memoryview(chunk)[start : start + CHUNK_BYTES]
                        await blocking(allocation.write, stream, part)
                        digest.update(part)
            if actual != size:
                raise ServiceError(422, "file_size_mismatch", "实际内容与预期大小不一致")
            value = digest.hexdigest()
            if checksum and checksum != value:
                raise ServiceError(422, "file_checksum_mismatch", "文件内容校验失败")
            yield StagedUpload(
                path, actual, value, capacity=allocation if destination is not None else None
            )

"""Bounded local transfer workspace, shared by API and Worker on one host.

Each permanent OS-locked slot owns one scratch file. A crashed process releases
its lock; the next holder removes its residue before reuse. No user paths enter
this namespace. Each slot gets an equal part of the configured disk budget.
"""

import asyncio
import hashlib
from contextlib import asynccontextmanager, contextmanager, nullcontext
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
    def __init__(self, coordination, settings, *, scope=None):
        self.coordination = coordination
        self.settings = settings
        self.capacity = Capacity(coordination, settings)
        self.scope = scope

    def scoped(self, scope):
        from keystone.platform.files.schemas.transfers import TransferScope

        return Transfers(
            self.coordination, self.settings, scope=TransferScope.model_validate(scope.model_dump())
        )

    @property
    def limit(self):
        return min(
            self.settings.file_max_bytes,
            self.settings.file_temp_bytes // self.settings.file_transfer_slots,
        )

    @contextmanager
    def slot(self):
        for number in range(self.settings.file_transfer_slots):
            lease = self.coordination.lease(f"transfer:{number}")
            try:
                lease.__enter__()
            except ServiceError as error:
                if error.code == "file_busy":
                    continue
                raise
            try:
                yield number
            finally:
                lease.__exit__(None, None, None)
            return
        raise ServiceError(503, "file_transfer_busy", "文件传输已满，请稍后重试")

    @asynccontextmanager
    async def read_slot(self, timeout=0):
        """Briefly queue HTTP reads; keep the same cross-process slot bound."""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            lease = self.slot()
            try:
                number = lease.__enter__()
            except ServiceError as error:
                remaining = deadline - asyncio.get_running_loop().time()
                if error.code != "file_transfer_busy" or remaining <= 0:
                    raise
                await asyncio.sleep(min(0.05, remaining))
                continue
            try:
                yield number
            finally:
                lease.__exit__(None, None, None)
            return

    @asynccontextmanager
    async def temporary(self, size, *, _slot=None):
        async with self.workspace(size, _slot=_slot) as workspace:
            yield workspace.path

    @asynccontextmanager
    async def workspace(self, size, *, _slot=None, destination=None):
        # _slot is only for host helpers already holding this registry's slot lease.
        if type(size) is not int or size < 0:
            raise ValueError("Invalid workspace byte promise")
        if size > self.limit:
            raise ServiceError(413, "file_temporary_limit", "文件超过临时空间配额")
        with self.slot() if _slot is None else nullcontext(_slot) as number:
            root = self.coordination.root
            path = root / ".transfers" / str(number)

            def prepare():
                self.coordination.fingerprint()
                directory = path.parent
                if not directory.resolve().is_relative_to(root):
                    raise ServiceError(503, "storage_unavailable", "临时存储路径无效")
                directory.mkdir(exist_ok=True)
                path.unlink(missing_ok=True)

            await blocking(prepare)
            targets = [(path.parent, size)]
            if destination is not None:
                targets.append((destination, size))

            def reserve():
                if self.scope is None:
                    return self.capacity.reserve(targets, slot=number)
                return self.capacity.reserve(
                    targets, slot=number, scope=self.scope, scope_bytes=size
                )

            async with opened(reserve) as allocation:
                workspace = Workspace(path, allocation)
                try:
                    async with monitor(workspace, size):
                        yield workspace
                finally:
                    await blocking(path.unlink, True)

    @asynccontextmanager
    async def stage(self, chunks, size, checksum=None, *, _slot=None, destination=None):
        async with self.workspace(size, _slot=_slot, destination=destination) as workspace:
            path, allocation = workspace.path, workspace.capacity
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

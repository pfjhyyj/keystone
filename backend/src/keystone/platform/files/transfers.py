"""Bounded local transfer workspace, shared by API and Worker on one host.

Each permanent OS-locked slot owns one scratch file. A crashed process releases
its lock; the next holder removes its residue before reuse. No user paths enter
this namespace. Each slot gets an equal part of the configured disk budget.
"""

import hashlib
import shutil
from contextlib import asynccontextmanager, contextmanager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event

from keystone.core.errors import ServiceError
from keystone.platform.files.storage import blocking, opened

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

    def __len__(self):
        return self.size


class Transfers:
    def __init__(self, coordination, settings):
        self.coordination = coordination
        self.settings = settings

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
    async def temporary(self, size, *, _slot=None):
        # _slot is only for host helpers already holding this registry's slot lease.
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
                if shutil.disk_usage(directory).free < size:
                    raise ServiceError(507, "file_temporary_full", "临时磁盘空间不足")

            await blocking(prepare)
            try:
                yield path
            finally:
                await blocking(path.unlink, True)

    @asynccontextmanager
    async def stage(self, chunks, size, checksum=None, *, _slot=None):
        async with self.temporary(size, _slot=_slot) as path:
            actual, digest = 0, hashlib.sha256()
            async with opened(path.open, "xb") as stream:
                async for chunk in chunks:
                    actual += len(chunk)
                    if actual > size:
                        raise ServiceError(413, "file_too_large", "上传内容超过声明大小")
                    # The ASGI server owns incoming chunk allocation; we make no aggregate copy.
                    for start in range(0, len(chunk), CHUNK_BYTES):
                        part = memoryview(chunk)[start : start + CHUNK_BYTES]
                        await blocking(stream.write, part)
                        digest.update(part)
            if actual != size:
                raise ServiceError(422, "file_size_mismatch", "实际内容与预期大小不一致")
            value = digest.hexdigest()
            if checksum and checksum != value:
                raise ServiceError(422, "file_checksum_mismatch", "文件内容校验失败")
            yield StagedUpload(path, actual, value)

"""Named OpenDAL filesystem/S3 sources with location guards."""

import asyncio
import hashlib
import json
import os
import re
from collections import OrderedDict
from contextlib import ExitStack, asynccontextmanager, nullcontext
from pathlib import Path

import opendal

from keystone.core import local_leases as leases
from keystone.core.asyncio import blocking as blocking
from keystone.core.errors import ServiceError
from keystone.core.storage_config import StorageConfig

LOCAL_WRITE_CHUNK_BYTES = 4 * 1024 * 1024


def initialize(root):
    leases.initialize(root)
    (Path(root).resolve() / "files").mkdir(exist_ok=True)


@asynccontextmanager
async def opened(function, *args, on_cancel=None):
    """Cancellation while opening must still close the handle created by the I/O thread."""
    handle = None

    def acquire():
        nonlocal handle
        handle = function(*args)

    try:
        await blocking(acquire, on_cancel=on_cancel)
        yield handle
    finally:
        if handle is not None:
            await blocking(handle.close)


class Storage:
    def __init__(self, config: StorageConfig):
        self.config = config
        self.root = self.config.root
        self.volume = leases.LocalLeases(self.root)
        self._verified = OrderedDict()
        self.capacity = None

    def fingerprint(self):
        if self.config.driver == "filesystem":
            return self.volume.fingerprint()
        location = [
            self.config.driver,
            self.config.endpoint,
            self.config.bucket,
            self.config.region,
            self.config.prefix,
        ]
        return hashlib.sha256(json.dumps(location).encode()).hexdigest()

    def check(self, fingerprint):
        if self.fingerprint() != fingerprint:
            raise leases.unavailable()

    def operator(self, fingerprint, key):
        self.check(fingerprint)
        if not re.fullmatch(r"files/[0-9a-f]{32}", key):
            raise leases.unavailable()
        if self.config.driver == "filesystem":
            if not (self.root / key).resolve().is_relative_to(self.root):
                raise leases.unavailable()
            return opendal.Operator("fs", root=str(self.root))
        return opendal.Operator(
            "s3",
            endpoint=self.config.endpoint,
            bucket=self.config.bucket,
            region=self.config.region,
            root=self.config.prefix,
            access_key_id=self.config.access_key.get_secret_value(),
            secret_access_key=self.config.secret_key.get_secret_value(),
            enable_virtual_host_style="false",
            disable_config_load="true",
            disable_ec2_metadata="true",
        ).layer(opendal.layers.TimeoutLayer(timeout=30.0))

    def ready(self):
        fingerprint = self.fingerprint()
        if self.config.driver == "s3":
            self.operator(fingerprint, "files/" + "0" * 32).layer(
                opendal.layers.TimeoutLayer(timeout=2.0)
            ).check()

    def write(self, fingerprint, key, data):
        try:
            from keystone.platform.files.adapters.transfers import StagedUpload

            op = self.operator(fingerprint, key)
            if self.config.driver == "filesystem" and self.capacity is not None:
                self._write_local(key, data)
                return
            if isinstance(data, StagedUpload):
                raise leases.unavailable()
            op.write(key, data)
            if op.stat(key).content_length != len(data):
                raise leases.unavailable()
        except ServiceError:
            raise
        except OSError as error:
            from keystone.platform.files.adapters.capacity import io_error

            raise io_error(error) from None
        except Exception:
            raise leases.unavailable() from None

    def _write_local(self, key, data):
        from keystone.platform.files.adapters.transfers import StagedUpload

        staged = isinstance(data, StagedUpload)
        existing = data.capacity if staged else None
        # Streamed uploads already reserved both copies before reading the body.
        # Small in-memory/internal uploads reserve their destination before opening it.
        with ExitStack() as stack:
            allocation = stack.enter_context(
                nullcontext(existing)
                if existing is not None
                else self.capacity.reserve([(self.root / "files", len(data))])
            )
            if staged:
                source = stack.enter_context(
                    nullcontext(data.stream) if data.stream is not None else data.path.open("rb")
                )
                chunks = iter(lambda: source.read(LOCAL_WRITE_CHUNK_BYTES), b"")
            else:
                content = memoryview(data)
                chunks = (
                    content[offset : offset + LOCAL_WRITE_CHUNK_BYTES]
                    for offset in range(0, len(content), LOCAL_WRITE_CHUNK_BYTES)
                )
            checksum, size = hashlib.sha256(), 0
            target = stack.enter_context((self.root / key).open("wb"))
            for chunk in chunks:
                if staged and data.cancelled.is_set():
                    raise leases.unavailable()
                size += len(chunk)
                if size > len(data):
                    raise leases.unavailable()
                allocation.write(target, chunk)
                checksum.update(chunk)
            target.flush()
            os.fsync(target.fileno())
            if (
                size != len(data)
                or (self.root / key).stat().st_size != size
                or (staged and checksum.hexdigest() != data.checksum)
            ):
                raise leases.unavailable()

    def open_local(self, fingerprint, key, expected_size, checksum, cancelled=None):
        """Return a verified local reader. Cache only unchanged filesystem identities.

        Every request checks the marker, key, size and stat signature. First access
        after restart/external change hashes incrementally; repeated video seeks
        do not scan the whole file. Managed active content is immutable.
        """
        from keystone.platform.files.adapters.transfers import checksum as content_checksum

        stream = None
        try:
            if self.config.driver != "filesystem":
                raise leases.unavailable()
            self.operator(fingerprint, key)
            path = self.root / key
            stream = path.open("rb")

            def signature():
                stat = os.fstat(stream.fileno())
                return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns

            before = signature()
            if before[2] != expected_size:
                raise leases.unavailable()
            identity = (fingerprint, key, before, checksum)
            if identity not in self._verified:
                try:
                    actual = content_checksum(stream, expected_size, cancelled)
                except ValueError:
                    raise ServiceError(503, "file_corrupt", "文件内容校验失败") from None
                if signature() != before or actual != checksum:
                    raise ServiceError(503, "file_corrupt", "文件内容校验失败")
                self._verified[identity] = True
                if len(self._verified) > 1024:
                    self._verified.popitem(last=False)
                stream.seek(0)
            return stream
        except Exception as error:
            if stream is not None:
                stream.close()
            if isinstance(error, ServiceError):
                raise
            raise leases.unavailable() from None

    def read(self, fingerprint, key, expected_size):
        try:
            operator = self.operator(fingerprint, key)
            if operator.stat(key).content_length != expected_size:
                raise leases.unavailable()
            return operator.read(key)
        except Exception:
            raise leases.unavailable() from None

    def delete(self, fingerprint, key):
        try:
            self.operator(fingerprint, key).delete(key)
        except Exception:
            raise leases.unavailable() from None

    def remove_residue(self, fingerprint, key):
        """Avoid creating a new S3 delete marker on every absent-object sweep."""
        try:
            operator = self.operator(fingerprint, key)
            if not operator.exists(key):
                return False
            operator.delete(key)
            return True
        except Exception:
            raise leases.unavailable() from None


class StorageRegistry:
    def __init__(self, settings):
        self.default_key = settings.file_default_storage
        self.sources = {key: Storage(config) for key, config in settings.file_storages.items()}
        self.coordination = leases.LocalLeases(settings.file_coordination_root)
        from keystone.platform.files.adapters.transfers import Transfers

        self.transfers = Transfers(self.coordination, settings)
        for source in self.sources.values():
            source.capacity = self.transfers.capacity

    def get(self, key=None):
        storage = self.sources.get(key if key is not None else self.default_key)
        if storage is None:
            raise ServiceError(503, "storage_unavailable", "文件存储未配置或暂不可用")
        return storage

    def lease(self, key, *, shared=False):
        return self.coordination.lease(key, shared=shared)

    def initialize(self):
        if not self.sources:
            raise ServiceError(
                503,
                "storage_unavailable",
                "文件存储未配置；请检查 keystone.toml 中的 file_storages、"
                "file_coordination_root 及当前工作目录",
            )
        roots = {self.coordination.root}
        roots.update(source.root for source in self.sources.values() if source.root is not None)
        for root in sorted(roots, key=lambda path: len(path.parts)):
            initialize(root)

    async def readiness(self):
        if not self.sources:
            return "unconfigured", {}

        async def check(source):
            try:
                async with asyncio.timeout(3):
                    await blocking(source.ready)
                return "ok"
            except Exception:
                return "unavailable"

        values = await asyncio.gather(*(check(source) for source in self.sources.values()))
        sources = dict(zip(self.sources, values, strict=True))
        try:
            self.coordination.fingerprint()
            status = sources[self.default_key]
        except Exception:
            status = "unavailable"
        return status, sources

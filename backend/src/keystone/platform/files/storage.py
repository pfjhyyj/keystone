"""Named OpenDAL filesystem/S3 sources; location guards adapted from Ether."""

import asyncio
import hashlib
import json
import os
import re
from collections import OrderedDict
from contextlib import asynccontextmanager

import opendal

from keystone.core.errors import ServiceError
from keystone.core.storage_config import StorageConfig
from keystone.platform.files import leases


async def blocking(function, *args, on_cancel=None):
    # Cancellation must not release the coordination lock while native I/O still runs.
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        if on_cancel is not None:
            on_cancel()
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                pass
            except Exception:
                # Native cleanup/abort errors must not replace the caller's cancellation.
                pass
        if not task.cancelled():
            task.exception()
        raise


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
            from keystone.platform.files.transfers import CHUNK_BYTES, StagedUpload

            op = self.operator(fingerprint, key)
            if isinstance(data, StagedUpload):
                if self.config.driver != "filesystem":
                    raise leases.unavailable()
                # Only uploading/failed objects can reach here, under their file lease.
                # A failed partial write stays unreadable and uses existing cleanup/retry.
                checksum, size = hashlib.sha256(), 0
                with data.path.open("rb") as source, (self.root / key).open("wb") as target:
                    while chunk := source.read(CHUNK_BYTES):
                        if data.cancelled.is_set():
                            raise leases.unavailable()
                        size += len(chunk)
                        if size > data.size:
                            raise leases.unavailable()
                        target.write(chunk)
                        checksum.update(chunk)
                    target.flush()
                    os.fsync(target.fileno())
                if (
                    size != data.size
                    or checksum.hexdigest() != data.checksum
                    or (self.root / key).stat().st_size != size
                ):
                    raise leases.unavailable()
                return
            op.write(key, data)
            if op.stat(key).content_length != len(data):
                raise leases.unavailable()
        except Exception:
            raise leases.unavailable() from None

    def open_local(self, fingerprint, key, expected_size, checksum, cancelled=None):
        """Return a verified local reader. Cache only unchanged filesystem identities.

        Every request checks the marker, key, size and stat signature. First access
        after restart/external change hashes incrementally; repeated video seeks
        do not scan the whole file. Managed active content is immutable.
        """
        from keystone.platform.files.transfers import checksum as content_checksum

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
        from keystone.platform.files.transfers import Transfers

        self.transfers = Transfers(self.coordination, settings)

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
            leases.initialize(root)

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

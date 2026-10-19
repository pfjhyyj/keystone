"""Named OpenDAL filesystem/S3 sources; location guards adapted from Ether."""

import asyncio
import hashlib
import json
import re

import opendal

from keystone.core.errors import ServiceError
from keystone.core.storage_config import StorageConfig
from keystone.platform.files import leases


async def blocking(function, *args):
    # Cancellation must not release the coordination lock while native I/O still runs.
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                pass
        if not task.cancelled():
            task.exception()
        raise


class Storage:
    def __init__(self, config: StorageConfig):
        self.config = config
        self.root = self.config.root
        self.volume = leases.LocalLeases(self.root)

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
            op = self.operator(fingerprint, key)
            op.write(key, data)
            if op.stat(key).content_length != len(data):
                raise leases.unavailable()
        except Exception:
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

    def get(self, key=None):
        storage = self.sources.get(key if key is not None else self.default_key)
        if storage is None:
            raise ServiceError(503, "storage_unavailable", "文件存储未配置或暂不可用")
        return storage

    def lease(self, key):
        return self.coordination.lease(key)

    def initialize(self):
        if not self.sources:
            raise leases.unavailable()
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

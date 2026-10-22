"""Shared single-host volume identity and permanent OS locks; no domain state."""

import hashlib
import re
import secrets
from contextlib import contextmanager
from pathlib import Path

import portalocker

from keystone.core.errors import ServiceError

MARKER = ".keystone-storage"


def unavailable():
    return ServiceError(503, "storage_unavailable", "文件存储暂不可用")


def initialize(root):
    if root is None:
        raise unavailable()
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    marker = root / MARKER
    if not marker.exists():
        if any(root.iterdir()):
            raise ServiceError(409, "storage_not_empty", "存储初始化要求空目录")
        try:
            with marker.open("x", encoding="utf-8") as stream:
                stream.write("keystone:" + secrets.token_hex(32))
        except FileExistsError:
            pass
    (root / ".locks").mkdir(exist_ok=True)
    LocalLeases(root).fingerprint()


class LocalLeases:
    def __init__(self, root):
        self.root = Path(root).resolve() if root else None
        self.identity = None

    def fingerprint(self):
        try:
            if self.root is None:
                raise unavailable()
            value = (self.root / MARKER).read_text(encoding="utf-8").strip()
            if not re.fullmatch(r"keystone:[0-9a-f]{64}", value):
                raise unavailable()
            identity = value.split(":")[1]
            if self.identity is not None and self.identity != identity:
                raise unavailable()
            self.identity = identity
            return identity
        except Exception:
            raise unavailable() from None

    @contextmanager
    def lease(self, key, *, shared=False):
        self.fingerprint()
        # Permanent lock files: unlinking one permits two independent lock owners.
        path = self.root / ".locks" / hashlib.sha256(str(key).encode()).hexdigest()
        try:
            handle = path.open("a+b")
        except OSError:
            raise unavailable() from None
        try:
            try:
                mode = portalocker.LOCK_SH if shared else portalocker.LOCK_EX
                portalocker.lock(handle, mode | portalocker.LOCK_NB)
            except portalocker.exceptions.LockException:
                raise ServiceError(409, "file_busy", "文件正在处理中，请稍后重试") from None
            yield
        finally:
            handle.close()

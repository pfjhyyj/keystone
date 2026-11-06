"""Scoped file operations; workspace and capacity handles stay inside Files."""

import hashlib
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from threading import Event

from keystone.core.errors import ServiceError
from keystone.platform.files.adapters.storage import blocking, opened
from keystone.platform.files.adapters.transfers import StagedUpload, checksum
from keystone.platform.files.schemas import Upload


class FileTransferService:
    """Created by FileService.transfer(); never persist or reuse outside that scope."""

    def __init__(self, files):
        self._files = files
        self._active, self._busy = True, False

    def close(self):
        self._active = False

    @contextmanager
    def _operation(self):
        if not self._active:
            raise RuntimeError("File transfer scope is closed")
        if self._busy:
            raise ServiceError(409, "file_scope_busy", "同一传输作用域已有文件操作")
        self._busy = True
        try:
            yield
        finally:
            self._busy = False

    @asynccontextmanager
    async def open_content(self, identifier, *, max_bytes=None):
        with self._operation():
            async with self._files.open_content(identifier, max_bytes=max_bytes) as reader:
                yield reader

    @asynccontextmanager
    async def workspace(self, max_bytes):
        with self._operation():
            async with self._files.identity.access():
                pass
            async with self._files.transfers.temporary(max_bytes) as path:
                yield path

    @asynccontextmanager
    async def materialize_file(self, identifier):
        with self._operation():
            transfers = self._files.transfers
            async with self._files.open_content(
                identifier, max_bytes=self._files.identity.settings.file_max_bytes
            ) as reader:
                async with transfers.workspace(reader.size) as workspace:
                    path = workspace.path / "input"
                    digest = hashlib.sha256()
                    async with opened(path.open, "xb") as target:
                        async for chunk in reader.chunks():
                            await blocking(workspace.capacity.write, target, chunk)
                            digest.update(chunk)
                    if digest.hexdigest() != reader.checksum:
                        raise ServiceError(503, "file_corrupt", "文件内容校验失败")
                    yield path

    async def upload_file(
        self,
        path,
        *,
        name,
        content_type,
        request_key,
        owner=None,
        slot=None,
        storage_key=None,
    ):
        """Trusted local source and reference keys; retain the existing upload transaction."""
        with self._operation():
            async with opened(Path(path).open, "rb") as source:
                size = await blocking(source.seek, 0, 2)
                self._files.prepare_upload(size, storage_key)
                await blocking(source.seek, 0)

                cancelled = Event()
                try:
                    digest = await blocking(
                        checksum, source, size, cancelled, on_cancel=cancelled.set
                    )
                except ValueError:
                    raise ServiceError(
                        422, "file_size_mismatch", "上传文件在校验时发生变化"
                    ) from None
                await blocking(source.seek, 0)

                data = Upload(
                    storage_key=storage_key,
                    name=name,
                    content_type=content_type,
                    expected_size=size,
                    checksum_sha256=digest,
                    request_key=request_key,
                )
                data = await self._files._resolved_upload(data)
                if self._files.storage.get(data.storage_key).config.driver != "filesystem":
                    raise ServiceError(
                        422, "local_storage_required", "本地文件保存需要本地文件存储"
                    )
                if (owner is None) != (slot is None) or (
                    owner is not None
                    and (not owner or len(owner) > 160 or not slot or len(slot) > 100)
                ):
                    raise ValueError("Invalid reference key")
                content = StagedUpload(Path(path), size, digest, cancelled=cancelled, stream=source)
                return await self._files._upload_content(
                    data, content, reference=(owner, slot) if owner is not None else None
                )

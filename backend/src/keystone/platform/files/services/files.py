import asyncio
import hashlib
import json
from contextlib import asynccontextmanager, contextmanager, nullcontext
from dataclasses import dataclass
from threading import Event
from uuid import uuid4

from sqlalchemy import select

from keystone.core.errors import ServiceError
from keystone.core.time import utcnow
from keystone.platform.audit.services import writer as audit
from keystone.platform.audit.services.writer import Actor
from keystone.platform.files.adapters.storage import blocking, opened
from keystone.platform.files.adapters.transfers import CHUNK_BYTES, StagedUpload
from keystone.platform.files.models import File, FileGrant
from keystone.platform.files.schemas import view
from keystone.platform.files.services import access as policy
from keystone.platform.files.services.references import require_unreferenced, retain_artifact
from keystone.platform.files.services.transfers import FileTransferService
from keystone.platform.identity.services.access import AccessContext


def digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


@dataclass
class ContentReader:
    name: str
    content_type: str
    size: int
    checksum: str
    stream: object

    async def chunks(self, start=0, length=None):
        await blocking(self.stream.seek, start)
        remaining = self.size - start if length is None else length
        while remaining:
            chunk = await blocking(self.stream.read, min(CHUNK_BYTES, remaining))
            if not chunk:
                raise ServiceError(503, "file_corrupt", "文件内容不完整")
            remaining -= len(chunk)
            yield chunk


class FileService:
    def __init__(self, identity: AccessContext, storage):
        self.identity, self.storage = identity, storage

    @contextmanager
    def transfer(self):
        """Acquire before a caller's execution scope; release after all I/O and cleanup."""
        with self.storage.transfers.slot() as number:
            transfer = FileTransferService(self, number)
            try:
                yield transfer
            finally:
                transfer.close()

    async def options(self):
        async with self.identity.access("file:create"):
            return {
                "max_file_bytes": self.identity.settings.file_max_bytes,
                "storage_keys": list(self.storage.sources),
                "default_storage": self.storage.default_key,
            }

    async def prepare_upload(self, expected_size, storage_key=None):
        async with self.identity.access("file:create"):
            if expected_size > self.identity.settings.file_max_bytes:
                raise ServiceError(413, "file_too_large", "文件超过上传上限")
            if storage_key is not None and storage_key not in self.storage.sources:
                raise ServiceError(422, "storage_unknown", "请选择已配置的文件存储源")
            if (
                storage_key is not None
                and self.storage.get(storage_key).config.driver != "filesystem"
                and expected_size > 64 * 1024 * 1024
            ):
                raise ServiceError(413, "file_too_large", "此存储源保留 64 MiB 上传限制")

    async def _resolved_upload(self, data):
        # A default-source change cannot retarget an already registered request key.
        async with self.identity.access("file:create") as (session, auth):
            previous = None
            if data.request_key:
                key = digest(["file.upload", str(auth.user.id), data.request_key])
                previous = await session.scalar(select(File).where(File.upload_key == key))
            storage_key = data.storage_key or (
                previous.storage_key if previous else self.storage.default_key
            )
        await self.prepare_upload(data.expected_size, storage_key)
        return data.model_copy(update={"storage_key": storage_key})

    async def upload_body(self, data, chunks):
        data = await self._resolved_upload(data)
        if self.storage.get(data.storage_key).config.driver == "filesystem":
            return await self.upload_stream(data, chunks)
        # Existing cloud upload remains bounded to 64 MiB; stage 0A adds no cloud protocol.
        content = bytearray()
        async for chunk in chunks:
            if len(content) + len(chunk) > data.expected_size:
                raise ServiceError(413, "file_too_large", "上传内容超过声明大小")
            content.extend(chunk)
        return await self.upload(data, bytes(content))

    async def upload_stream(self, data, chunks, *, owner=None, slot=None, _transfer_slot=None):
        """Local-only streaming; ordinary upload remains available to bounded callers."""
        data = await self._resolved_upload(data)
        if self.storage.get(data.storage_key).config.driver != "filesystem":
            raise ServiceError(422, "local_storage_required", "流式上传需要本地文件存储")
        if (owner is None) != (slot is None) or (
            owner is not None and (not owner or len(owner) > 160 or not slot or len(slot) > 100)
        ):
            raise ValueError("Invalid reference key")
        reference = (owner, slot) if owner is not None else None
        async with self.storage.transfers.stage(
            chunks,
            data.expected_size,
            data.checksum_sha256,
            _slot=_transfer_slot,
            destination=self.storage.get(data.storage_key).root / "files",
        ) as staged:
            return await self._upload_content(data, staged, reference=reference)

    async def listing(self, offset=0, limit=50):
        async with self.identity.access("file:inspect") as (session, auth):
            return [
                view(row)
                for row in await session.scalars(
                    select(File)
                    .where(policy.scope(auth, "inspect"))
                    .order_by(File.created_at.desc(), File.id)
                    .offset(offset)
                    .limit(limit)
                )
            ]

    async def detail(self, identifier):
        async with self.identity.access("file:inspect") as (session, auth):
            file = await session.get(File, identifier)
            await policy.require(session, auth, file, "inspect")
            return view(file)

    async def upload(self, data, content):
        return await self._upload_content(data, content)

    async def upload_referenced(self, data, content, *, owner, slot):
        """Trusted business service only: owner/slot are fixed by the caller, never HTTP input."""
        if not owner or len(owner) > 160 or not slot or len(slot) > 100:
            raise ValueError("Invalid reference key")
        return await self._upload_content(data, content, reference=(owner, slot))

    async def _upload_content(self, data, content, reference=None):
        if len(content) != data.expected_size:
            raise ServiceError(422, "file_size_mismatch", "实际内容与预期大小不一致")
        if len(content) > self.identity.settings.file_max_bytes:
            raise ServiceError(413, "file_too_large", "文件超过上传上限")
        checksum = (
            content.checksum
            if isinstance(content, StagedUpload)
            else hashlib.sha256(content).hexdigest()
        )
        if data.checksum_sha256 and checksum != data.checksum_sha256:
            raise ServiceError(422, "file_checksum_mismatch", "文件内容校验失败")
        async with self.identity.access("file:create") as (_, auth):
            user_id = auth.user.id
            if data.storage_key is not None and data.storage_key not in self.storage.sources:
                raise ServiceError(422, "storage_unknown", "请选择已配置的文件存储源")
        key = digest(["file.upload", str(user_id), data.request_key or str(uuid4())])
        metadata = [data.name, data.content_type, data.expected_size, data.checksum_sha256]
        # Serialize same upload request before discovering its stable file ID.
        with self.storage.lease("upload:" + key):
            async with self.identity.database.transaction() as session:
                previous = await session.scalar(select(File).where(File.upload_key == key))
                identifier = previous.id if previous else uuid4()
                storage_key = data.storage_key or (
                    previous.storage_key if previous else self.storage.default_key
                )
            storage = self.storage.get(storage_key)
            fingerprint = await blocking(storage.fingerprint)
            original = digest([*metadata, storage_key, fingerprint])
            with self.storage.lease(identifier):
                return await self._upload(
                    identifier,
                    key,
                    original,
                    data,
                    content,
                    checksum,
                    user_id,
                    reference,
                    storage_key,
                    fingerprint,
                )

    async def _upload(
        self,
        identifier,
        key,
        original,
        data,
        content,
        checksum,
        user_id,
        reference,
        storage_key,
        fingerprint,
    ):
        storage = self.storage.get(storage_key)
        async with self.identity.access(
            "file:create", action="file.upload.start", target="file", target_id=identifier
        ) as (session, auth):
            file = await session.get(File, identifier, with_for_update=True)
            created = file is None
            if file is not None:
                await policy.require(session, auth, file, "manage")
                if file.storage_key != storage_key:
                    raise ServiceError(409, "idempotency_conflict", "同一上传请求键对应不同存储源")
                if file.storage_fingerprint != fingerprint:
                    raise ServiceError(503, "storage_unavailable", "文件存储身份不匹配")
                if file.upload_digest != original:
                    raise ServiceError(409, "idempotency_conflict", "同一上传请求键对应不同内容")
                if file.status == "active":
                    await policy.require(session, auth, file, "inspect")
                    await self._retain_upload(session, file, user_id, key, reference)
                    return view(file)
                if file.status not in {"failed", "uploading"}:
                    raise ServiceError(409, "upload_unavailable", "该文件不能重试上传")
                file.status = "uploading"
                self.identity.touch(file, auth)
            else:
                file = File(
                    id=identifier,
                    name=data.name,
                    owner_id=auth.user.id,
                    content_type=data.content_type,
                    upload_key=key,
                    upload_digest=original,
                    storage_key=storage_key,
                    object_key=f"files/{identifier.hex}",
                    storage_fingerprint=fingerprint,
                    status="uploading",
                    created_by=auth.user.id,
                    updated_by=auth.user.id,
                )
                session.add(file)
                await session.flush()
                session.add_all(
                    FileGrant(
                        file_id=identifier,
                        subject_type="user",
                        user_id=auth.user.id,
                        action=action,
                        created_by=auth.user.id,
                    )
                    for action in ("inspect", "content_read", "manage")
                )
            await self.identity.log(
                session,
                auth,
                "file.upload.start",
                "file",
                identifier,
                {"initial_grants": ["inspect", "content_read", "manage"] if created else []},
            )
            fingerprint, object_key = file.storage_fingerprint, file.object_key
        try:
            # Same-key retries can only write identical validated bytes. This also makes
            # delayed S3 PUTs harmless to a completed retry; deletion uses tombstone sweeps.
            if isinstance(content, StagedUpload):
                await blocking(
                    storage.write, fingerprint, object_key, content, on_cancel=content.cancelled.set
                )
            else:
                await blocking(storage.write, fingerprint, object_key, content)
            async with self.identity.database.transaction() as session:
                file = await policy.locked(session, identifier)
                if file.status == "uploading":
                    file.status, file.size_bytes, file.checksum_sha256 = (
                        "active",
                        len(content),
                        checksum,
                    )
                    file.updated_at, file.updated_by = utcnow(), None
                    await self._retain_upload(session, file, user_id, key, reference)
                    await audit.record(
                        session,
                        "file.upload.complete",
                        Actor("system"),
                        "file",
                        identifier,
                        correlation_id=self.identity.correlation_id,
                        details={"size_bytes": len(content)},
                    )
                elif file.status != "deleting":
                    raise ServiceError(409, "upload_unavailable", "文件状态已改变")
        except BaseException:
            # The registered object remains recoverable even if DB/storage is unavailable.
            await self._fail_upload(identifier)
            raise
        # A create-only caller gets a receipt, not permission to query metadata.
        # Even receipts must not escape after the initial object grant is revoked.
        async with self.identity.access("file:create") as (session, auth):
            file = await session.scalar(
                select(File).where(File.id == identifier, policy.scope(auth, "inspect"))
            )
            if file is None:
                raise ServiceError(404, "file_not_found", "文件不存在或不可访问")
            return view(file) if "file:inspect" in auth.permissions else {"id": file.id}

    async def _retain_upload(self, session, file, user_id, key, reference):
        if reference is None:
            return
        if file.owner_id != user_id or file.upload_key != key or file.status != "active":
            raise ServiceError(409, "file_reference_invalid", "文件不属于本次受控上传")
        owner, slot = reference
        await retain_artifact(session, owner, file, slot)

    async def _fail_upload(self, identifier):
        async def mark():
            try:
                async with asyncio.timeout(5), self.identity.database.transaction() as session:
                    file = await policy.locked(session, identifier)
                    if file.status == "uploading":
                        file.status, file.updated_at, file.updated_by = "failed", utcnow(), None
                        await audit.record(
                            session,
                            "file.upload.failure",
                            Actor("system"),
                            "file",
                            identifier,
                            outcome="failure",
                            correlation_id=self.identity.correlation_id,
                            details={"reason": "upload_interrupted"},
                        )
            except Exception:
                # Worker recovers abandoned uploading rows; no unregistered content exists.
                import logging

                logging.getLogger(__name__).error(
                    "file_upload_recovery_pending file_id=%s", identifier
                )

        # A disk/Run monitor may cancel the upload while its failure transaction
        # is pending. Finish this bounded, known task before releasing file leases;
        # repeated cancellation must not leave an otherwise writable row uploading.
        settlement = asyncio.create_task(mark())
        interrupted = False
        while not settlement.done():
            try:
                await asyncio.shield(settlement)
            except asyncio.CancelledError:
                interrupted = True
        settlement.result()
        if interrupted:
            raise asyncio.CancelledError()

    async def content(self, identifier, *, max_bytes=64 * 1024 * 1024):
        if max_bytes is None or max_bytes < 0:
            raise ValueError("max_bytes must be non-negative")
        # Fast authorization before probing storage, then recheck under the lease.
        async with self.identity.access("file:content_read") as (session, auth):
            await policy.require(session, auth, await session.get(File, identifier), "content_read")
        with self.storage.lease(identifier, shared=True):
            async with self.identity.access("file:content_read") as (session, auth):
                file = await session.get(File, identifier)
                await policy.require(session, auth, file, "content_read")
                fingerprint, key = file.storage_fingerprint, file.object_key
                storage = self.storage.get(file.storage_key)
                expected = file.checksum_sha256
                expected_size = file.size_bytes
                if max_bytes is not None and expected_size > max_bytes:
                    raise ServiceError(413, "file_too_large", "文件超过读取上限")
            content = await blocking(storage.read, fingerprint, key, expected_size)
            if hashlib.sha256(content).hexdigest() != expected:
                raise ServiceError(503, "file_corrupt", "文件内容校验失败")
            async with self.identity.access("file:content_read") as (session, auth):
                file = await session.get(File, identifier)
                await policy.require(session, auth, file, "content_read")
                return file.name, content

    @asynccontextmanager
    async def open_content(
        self, identifier, *, _transfer_slot=None, max_bytes=None, wait_for_slot=False
    ):
        """Hold the object lease until transport/Worker closes the reader; no DB I/O lock."""
        async with self.identity.access("file:content_read") as (session, auth):
            await policy.require(session, auth, await session.get(File, identifier), "content_read")
        slot = (
            self.storage.transfers.read_slot(timeout=3 if wait_for_slot else 0)
            if _transfer_slot is None
            else nullcontext()
        )
        async with slot:
            with self.storage.lease(identifier, shared=True):
                async with self.identity.access("file:content_read") as (session, auth):
                    file = await session.get(File, identifier)
                    await policy.require(session, auth, file, "content_read")
                    source = self.storage.get(file.storage_key)
                    metadata = file.name, file.content_type, file.size_bytes, file.checksum_sha256
                    fingerprint, key = file.storage_fingerprint, file.object_key
                    if max_bytes is not None and file.size_bytes > max_bytes:
                        raise ServiceError(413, "file_too_large", "文件超过读取上限")

                cancelled = Event()

                def acquire():
                    if source.config.driver == "filesystem":
                        return source.open_local(
                            fingerprint, key, metadata[2], metadata[3], cancelled
                        )
                    # Preserve the existing bounded S3 path; cloud streaming is stage 0B.
                    from io import BytesIO

                    if metadata[2] > 64 * 1024 * 1024:
                        raise ServiceError(413, "file_too_large", "此存储源保留 64 MiB 读取限制")
                    body = source.read(fingerprint, key, metadata[2])
                    if hashlib.sha256(body).hexdigest() != metadata[3]:
                        raise ServiceError(503, "file_corrupt", "文件内容校验失败")
                    return BytesIO(body)

                async with opened(acquire, on_cancel=cancelled.set) as stream:
                    async with self.identity.access("file:content_read") as (session, auth):
                        await policy.require(
                            session, auth, await session.get(File, identifier), "content_read"
                        )
                    yield ContentReader(*metadata, stream)

    async def edit(self, identifier, data):
        async with self.identity.access(
            "file:manage", action="file.update", target="file", target_id=identifier
        ) as (session, auth):
            file = await policy.locked(session, identifier)
            await policy.require(session, auth, file, "manage")
            policy.editable(file)
            before = {"name": file.name, "status": file.status}
            file.name, file.status = data.name, data.status
            self.identity.touch(file, auth)
            await self.identity.log(
                session,
                auth,
                "file.update",
                "file",
                identifier,
                {"before": before, "after": data.model_dump()},
            )
            return {"id": file.id}

    async def request_delete(self, identifier):
        async with self.identity.access(
            "file:manage", action="file.delete.request", target="file", target_id=identifier
        ) as (session, auth):
            return await self.request_delete_in_transaction(session, auth, identifier)

    async def request_delete_in_transaction(self, session, auth, identifier):
        """Accept deletion in the caller's current authorized business transaction.

        Release approved references first, locking the complete affected file set in
        ID order. Any error must roll back the caller's transaction. No commit, storage
        I/O or task dispatch; existing maintenance owns physical deletion and grants.
        """
        auth.require("file:manage")
        file = await policy.locked(session, identifier)
        await policy.require(session, auth, file, "manage")
        if file.status not in {"deleting", "deleted"}:
            await require_unreferenced(session, identifier)
            file.status = "deleting"
            self.identity.touch(file, auth)
            await self.identity.log(session, auth, "file.delete.request", "file", identifier)
        return {"id": file.id}

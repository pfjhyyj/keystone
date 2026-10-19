"""Trusted business integration; no public arbitrary-owner write endpoint."""

import hashlib
from uuid import UUID

from sqlalchemy import delete, select, text

from keystone.core.errors import ServiceError
from keystone.platform.audit.services import writer as audit
from keystone.platform.audit.services.writer import Actor, record
from keystone.platform.files import policy
from keystone.platform.files.models import File, FileReference
from keystone.platform.identity.access import AccessContext


async def require_unreferenced(session, identifier):
    if await session.scalar(
        select(FileReference.file_id).where(FileReference.file_id == identifier).limit(1)
    ):
        raise ServiceError(409, "file_referenced", "文件仍被业务引用，不能删除")


class FileReferences:
    def __init__(self, session, auth, namespace, *, correlation_id=None):
        if not namespace or ":" in namespace or len(namespace) > 32:
            raise ValueError("Invalid business namespace")
        self.session, self.auth, self.namespace = session, auth, namespace
        self.correlation_id = correlation_id

    async def replace(self, owner, slot, identifiers):
        """Call after business authorization, in its transaction holding identity_lock.

        Owner/slot and existing business state are trusted service inputs, not request switches.
        """
        key = f"{self.namespace}:{owner}"
        if not owner or len(key) > 160 or not slot or len(slot) > 100:
            raise ValueError("Invalid reference key")
        ids = {UUID(str(value)) for value in identifiers}
        if len(ids) > 100:
            raise ValueError("Too many references")
        lock = int.from_bytes(hashlib.sha256(f"{key}:{slot}".encode()).digest()[:4], signed=True)
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(73190422, :key)"), {"key": lock}
        )
        condition = (FileReference.owner == key, FileReference.slot == slot)
        old = set(await self.session.scalars(select(FileReference.file_id).where(*condition)))
        # Sorted row locks coordinate both reference writers and deletion. Unchanged rows stay.
        for identifier in sorted(old ^ ids):
            file = await policy.locked(self.session, identifier)
            if identifier in ids:
                await policy.require(self.session, self.auth, file, "content_read")
                self.session.add(FileReference(owner=key, slot=slot, file_id=identifier))
            else:
                await self.session.execute(
                    delete(FileReference).where(*condition, FileReference.file_id == identifier)
                )
            details = {"owner": key, "slot": slot, "added": identifier in ids}
            if self.auth.kind == "pat":
                details["credential_id"] = str(self.auth.credential.id)
            await record(
                self.session,
                "file.reference",
                Actor("user", self.auth.user.id),
                "file",
                identifier,
                correlation_id=self.correlation_id,
                details=details,
            )
        await self.session.flush()

    async def release(self, owner, slot):
        await self.replace(owner, slot, ())


class FileReferenceService:
    def __init__(self, identity: AccessContext):
        self.identity = identity

    async def listing(self, identifier, offset=0, limit=50):
        async with self.identity.access("file:manage") as (session, auth):
            await policy.require(session, auth, await session.get(File, identifier), "manage")
            return [
                {"owner": row.owner, "slot": row.slot}
                for row in await session.scalars(
                    select(FileReference)
                    .where(FileReference.file_id == identifier)
                    .order_by(FileReference.owner, FileReference.slot)
                    .offset(offset)
                    .limit(limit)
                )
            ]


async def retain_artifact(session, owner, file, slot):
    """Trusted integration only; caller has already authorized this artifact and owns the lock."""
    exists = await session.get(FileReference, (owner, slot, file.id))
    if exists is None:
        session.add(FileReference(owner=owner, slot=slot, file_id=file.id))
        await audit.record(
            session,
            "file.reference",
            Actor("system"),
            "file",
            file.id,
            details={"owner": owner, "slot": slot, "added": True},
        )


async def retain_registered_output(session, owner, identifier):
    """Require an existing usage record; never grant access to arbitrary output IDs."""
    file = await policy.locked(session, identifier)
    registered = await session.scalar(
        select(FileReference.file_id)
        .where(FileReference.owner == owner, FileReference.file_id == identifier)
        .limit(1)
    )
    if registered is None or file.status not in {"active", "disabled"}:
        raise ServiceError(409, "file_reference_invalid", "文件没有当前业务的有效使用记录")
    await retain_artifact(session, owner, file, "output")

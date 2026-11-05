"""Trusted business integration; no public arbitrary-owner write endpoint."""

import hashlib
from uuid import UUID

from sqlalchemy import delete, func, select, text, tuple_

from keystone.core.errors import ServiceError
from keystone.core.time import utcnow
from keystone.platform.audit.services import writer as audit
from keystone.platform.audit.services.writer import Actor, record
from keystone.platform.files.models import File, FileReference
from keystone.platform.files.schemas.diagnostics import ReferenceDiagnostic, ReferenceDiagnostics
from keystone.platform.files.services import access as policy
from keystone.platform.identity.services.access import AccessContext


async def require_unreferenced(session, identifier):
    if await session.scalar(
        select(FileReference.file_id).where(FileReference.file_id == identifier).limit(1)
    ):
        raise ServiceError(409, "file_referenced", "文件仍被业务引用，不能删除")


async def registered_file_ids_many(session, owner, slots):
    """Exact bounded slots; two matches per slot suffice to detect ambiguity."""
    keys = set(slots)
    if len(keys) > 100:
        raise ValueError("At most 100 slots are supported")
    matches = (
        select(
            FileReference.slot,
            FileReference.file_id,
            func.row_number()
            .over(partition_by=FileReference.slot, order_by=FileReference.file_id)
            .label("position"),
        )
        .where(FileReference.owner == owner, FileReference.slot.in_(keys))
        .subquery()
    )
    rows = await session.execute(
        select(matches.c.slot, matches.c.file_id)
        .where(matches.c.position <= 2)
        .order_by(matches.c.slot, matches.c.file_id)
    )
    result = {key: [] for key in keys}
    for slot, identifier in rows:
        result[slot].append(identifier)
    return {key: tuple(values) for key, values in result.items()}


async def registered_file_ids(session, owner, slot):
    """Trusted, exact reference lookup. Two IDs are enough to detect an ambiguous slot.

    The calling domain authorizes owner/slot; this is not an arbitrary-owner API.
    Return IDs only, without exposing storage locations or reference ORM rows.
    """
    return tuple(
        await session.scalars(
            select(FileReference.file_id)
            .where(FileReference.owner == owner, FileReference.slot == slot)
            .order_by(FileReference.file_id)
            .limit(2)
        )
    )


async def require_registered_file(session, owner, slot, identifier):
    """Caller holds its identity/business locks; serialize with file/reference changes.

    For several files call in file-ID order. No commit or storage I/O.
    """
    file = await policy.locked(session, identifier)
    if await registered_file_ids(session, owner, slot) != (identifier,):
        raise ServiceError(409, "file_reference_invalid", "文件没有当前业务的唯一使用记录")
    policy.available(file, content=True)


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
                policy.available(file, content=True)
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

    async def claims_in_transaction(self, session, owners):
        """Trusted caller authorizes each exact business/Run owner before lookup.

        Bounded ID/slot projection only. No HTTP owner selector, storage location,
        content grant or deletion authority; the caller still approves retirement.
        """
        keys = set(owners)
        if len(keys) > 500 or any(not isinstance(k, str) or not 1 <= len(k) <= 160 for k in keys):
            raise ServiceError(422, "file_reference_limit", "引用核对范围过大")
        rows = list(
            (
                await session.execute(
                    select(FileReference.owner, FileReference.slot, FileReference.file_id)
                    .where(FileReference.owner.in_(keys))
                    .order_by(FileReference.file_id, FileReference.owner, FileReference.slot)
                    .limit(5001)
                )
            ).all()
        )
        if len(rows) > 5000:
            raise ServiceError(422, "file_reference_limit", "引用核对范围过大")
        return [tuple(row) for row in rows]

    @staticmethod
    def validate_retirement_scope(references, artifacts):
        """Normalize a bounded, exact maintenance manifest before acquiring leases."""
        raw_claims, raw_ids = set(references), set(artifacts)
        if len(raw_claims) > 5000 or len(raw_ids) > 500:
            raise ServiceError(422, "file_reference_limit", "清理范围过大")
        claims, ids = set(), {UUID(str(value)) for value in raw_ids}
        for owner, slot, identifier in raw_claims:
            if (
                not isinstance(owner, str)
                or not 1 <= len(owner) <= 160
                or not isinstance(slot, str)
                or not 1 <= len(slot) <= 100
            ):
                raise ValueError("Invalid reference key")
            claims.add((owner, slot, UUID(str(identifier))))
        if len(ids | {claim[2] for claim in claims}) > 1000:
            raise ServiceError(422, "file_reference_limit", "清理范围过大")
        return claims, ids

    async def retire_in_transaction(self, session, *, references, artifacts, owner_id):
        """Trusted system maintenance only, never an HTTP or user-authorized shortcut.

        Caller proves business expiry/terminal execution under its business locks,
        and holds exclusive artifact OS leases through transaction commit. This
        method knows no expiry policy and performs no storage I/O or commit.
        Foreign usage, wrong ownership or unproven active artifacts defer the whole
        group. Input claims may be released without deleting their content.
        """
        claims, ids = self.validate_retirement_scope(references, artifacts)
        owner_id = UUID(str(owner_id))
        files = {}
        for identifier in sorted(ids | {claim[2] for claim in claims}):
            file = await policy.locked(session, identifier)
            if identifier in ids:
                if file.owner_id != owner_id or file.status not in {
                    "active",
                    "disabled",
                    "deleting",
                    "deleted",
                }:
                    return False
                files[identifier] = file
        statement = select(FileReference.file_id).where(FileReference.file_id.in_(ids))
        if claims:
            statement = statement.where(
                tuple_(FileReference.owner, FileReference.slot, FileReference.file_id).not_in(
                    claims
                )
            )
        if await session.scalar(statement.limit(1)):
            return False
        proven = set(
            await session.scalars(
                select(FileReference.file_id).where(FileReference.file_id.in_(ids)).distinct()
            )
        )
        if any(f.status in {"active", "disabled"} and f.id not in proven for f in files.values()):
            return False
        for owner, slot, identifier in sorted(claims):
            result = await session.execute(
                delete(FileReference).where(
                    FileReference.owner == owner,
                    FileReference.slot == slot,
                    FileReference.file_id == identifier,
                )
            )
            if result.rowcount:
                await audit.record(
                    session,
                    "file.reference",
                    Actor("system"),
                    "file",
                    identifier,
                    details={"owner": owner, "slot": slot, "added": False},
                )
        for identifier, file in files.items():
            if file.status in {"active", "disabled"}:
                file.status, file.updated_at, file.updated_by = "deleting", utcnow(), None
                await audit.record(
                    session, "file.delete.request", Actor("system"), "file", identifier
                )
        return True

    async def flow_diagnostics_in_transaction(self, session, run_id, offset, limit):
        """Trusted caller authorizes this Run under its current identity transaction.

        References never supply inspect or content grants. Filter before pagination.
        """
        rows = list(
            await session.execute(
                select(
                    FileReference.file_id,
                    FileReference.slot,
                    File.checksum_sha256,
                    File.size_bytes,
                    File.content_type,
                )
                .join(File, File.id == FileReference.file_id)
                .where(FileReference.owner == f"flow:{run_id}")
                .order_by(FileReference.slot, FileReference.file_id)
                .offset(offset)
                .limit(limit + 1)
            )
        )
        truncated = len(rows) > limit
        return ReferenceDiagnostics(
            items=[
                ReferenceDiagnostic(
                    file_id=row.file_id,
                    slot=row.slot,
                    checksum_sha256=row.checksum_sha256,
                    size_bytes=row.size_bytes,
                    content_type=row.content_type,
                )
                for row in rows[:limit]
            ],
            truncated=truncated,
            next_offset=offset + limit if truncated else None,
        )

    async def release_in_transaction(self, session, auth, references):
        """Release exact (owner, slot, file_id) claims approved by trusted business code.

        The caller supplies current AccessContext auth and authorizes every owner;
        these are never client-controlled selectors. Lock all affected files in ID
        order before other file locks, including retained input files. No content or
        manage grant is needed merely to release one's authorized business usage.
        No commit, storage I/O or deletion; missing claims are idempotent no-ops.
        """
        claims = set()
        for owner, slot, identifier in references:
            if (
                not isinstance(owner, str)
                or not 1 <= len(owner) <= 160
                or not isinstance(slot, str)
                or not 1 <= len(slot) <= 100
            ):
                raise ValueError("Invalid reference key")
            claims.add((owner, slot, UUID(str(identifier))))
        for identifier in sorted({claim[2] for claim in claims}):
            await policy.locked(session, identifier)
        removed = 0
        for owner, slot, identifier in sorted(claims):
            result = await session.execute(
                delete(FileReference).where(
                    FileReference.owner == owner,
                    FileReference.slot == slot,
                    FileReference.file_id == identifier,
                )
            )
            if result.rowcount:
                removed += result.rowcount
                await self.identity.log(
                    session,
                    auth,
                    "file.reference",
                    "file",
                    identifier,
                    {"owner": owner, "slot": slot, "added": False},
                )
        return removed


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


async def require_usage(session, owner, identifier):
    """Trusted caller authorizes owner; content authorization remains a separate check."""
    exists = await session.scalar(
        select(FileReference.file_id)
        .where(FileReference.owner == owner, FileReference.file_id == identifier)
        .limit(1)
    )
    if exists is None:
        raise ServiceError(403, "file_usage_denied", "文件没有当前业务的使用记录")


async def describe_inputs_in_transaction(session, identifiers):
    """Caller holds its identity transaction. Read immutable file sizes before retaining references.

    The caller must retain/revalidate references in the same transaction afterwards.
    No storage access, write or independent commit; no File ORM crosses the boundary.
    """
    values = []
    for identifier in sorted({UUID(str(value)) for value in identifiers}):
        file = await session.get(File, identifier)
        policy.available(file, content=True)
        values.append(
            {
                "id": str(identifier),
                "size_bytes": file.size_bytes,
                "checksum_sha256": file.checksum_sha256,
            }
        )
    return values

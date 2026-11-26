from datetime import timedelta

from sqlalchemy import false, func, select

from keystone.core.errors import ServiceError
from keystone.core.time import utcnow
from keystone.platform.audit.services import writer as audit
from keystone.platform.audit.services.writer import Actor
from keystone.platform.files.adapters.coordination import file_lease
from keystone.platform.files.adapters.storage import blocking
from keystone.platform.files.models import File, FileReference
from keystone.platform.files.services import access as policy
from keystone.platform.files.services.references import require_unreferenced

MAX_DELETE_FAILURES = 3
DELETE_RETRY_DELAY = timedelta(minutes=1)
MAX_RESIDUE_CHECKS = 3


def pending_delete():
    return (File.status == "deleting") & (File.delete_failures < MAX_DELETE_FAILURES)


def exhausted_delete():
    return (File.status == "deleting") & (File.delete_failures >= MAX_DELETE_FAILURES)


def residue_due(cutoff, remote_keys):
    # A timed-out PUT may land after failure or deletion; a few late checks cover it.
    return (
        (File.residue_checks < MAX_RESIDUE_CHECKS)
        & (File.updated_at < cutoff)
        & (
            (File.status == "failed")
            | ((File.status == "deleted") & File.storage_key.in_(remote_keys))
        )
    )


async def retry_delete(database, identifier):
    """Requeue an exhausted deletion by its registered File ID."""
    async with database.transaction() as session:
        file = await policy.locked(session, identifier)
        if file.status != "deleting" or file.delete_failures < MAX_DELETE_FAILURES:
            raise ServiceError(409, "file_delete_not_exhausted", "文件删除尚未达到自动尝试上限")
        await require_unreferenced(session, identifier)
        file.delete_failures = 0
        file.updated_at, file.updated_by = utcnow(), None
        await audit.record(
            session,
            "file.delete.request",
            Actor("system"),
            "file",
            identifier,
            details={"reason": "operator_retry"},
        )


def expired_unreferenced(grace_seconds):
    if grace_seconds is None:
        return false()
    if type(grace_seconds) is not int or grace_seconds < 1:
        raise ValueError("File reclamation grace must be positive")
    return (
        (File.status == "active")
        & (File.completed_at <= utcnow() - timedelta(seconds=grace_seconds))
        & ~select(FileReference.file_id).where(FileReference.file_id == File.id).exists()
    )


async def backlog(database, *, unreferenced_grace_seconds=None):
    """Pending work plus exhausted deletions for operators; residue checks are not backlog."""
    cutoff = utcnow() - timedelta(hours=1)
    async with database.transaction() as session:
        pending, oldest = (
            await session.execute(
                select(func.count(), func.min(File.updated_at)).where(
                    pending_delete()
                    | ((File.status == "uploading") & (File.updated_at < cutoff))
                    | expired_unreferenced(unreferenced_grace_seconds)
                )
            )
        ).one()
        exhausted = await session.scalar(select(func.count()).where(exhausted_delete()))
        return pending, oldest, {"exhausted": exhausted}


async def cleanup(database, storage, *, limit=20, unreferenced_grace_seconds=None):
    """Bounded pass; unreferenced active reclamation requires an explicit grace period."""
    now = utcnow()
    cutoff, retry_cutoff = now - timedelta(hours=1), now - DELETE_RETRY_DELAY
    remote_keys = [key for key, source in storage.sources.items() if source.config.driver == "s3"]
    async with database.transaction() as session:
        identifiers = list(
            await session.scalars(
                select(File.id)
                .where(
                    (
                        pending_delete()
                        & ((File.delete_failures == 0) | (File.updated_at <= retry_cutoff))
                    )
                    | expired_unreferenced(unreferenced_grace_seconds)
                    | ((File.status == "uploading") & (File.updated_at < cutoff))
                    | residue_due(cutoff, remote_keys)
                )
                # Requested deletions go first; recurring residue sweeps cannot starve them.
                .order_by(File.status != "deleting", File.updated_at, File.id)
                .limit(limit)
            )
        )
    for identifier in identifiers:
        try:
            async with file_lease(database, storage, identifier) as lease:
                transaction = lease.transaction
                try:
                    await _cleanup_file(
                        transaction,
                        storage,
                        identifier,
                        cutoff,
                        retry_cutoff,
                        unreferenced_grace_seconds,
                        remote_keys,
                    )
                except ServiceError as error:
                    if error.code in {"file_busy", "file_coordination_lost"}:
                        raise
                    await _cleanup_failure(transaction, identifier, error)
        except ServiceError as error:
            if error.code != "file_busy":
                raise


async def _cleanup_file(transaction, storage, identifier, cutoff, retry_cutoff, grace, remote_keys):
    async with transaction() as session:
        file = await policy.locked(session, identifier)
        if file.status == "deleting" and (
            file.delete_failures >= MAX_DELETE_FAILURES
            or (file.delete_failures and file.updated_at > retry_cutoff)
        ):
            return
        if file.status in {"failed", "deleted"} and (
            file.residue_checks >= MAX_RESIDUE_CHECKS
            or file.updated_at >= cutoff
            or (file.status == "deleted" and file.storage_key not in remote_keys)
        ):
            return
        # Reference creation locks this same row. Recheck after admission,
        # then commit deleting before performing any external deletion.
        if file.status == "active":
            eligible = await session.scalar(
                select(File.id).where(File.id == identifier, expired_unreferenced(grace))
            )
            if eligible is None:
                return
            file.status, file.updated_at, file.updated_by = "deleting", utcnow(), None
            await audit.record(
                session,
                "file.delete.request",
                Actor("system"),
                "file",
                identifier,
                details={"reason": "unreferenced_expired"},
            )
        if file.status == "uploading" and file.updated_at < cutoff:
            file.status, file.updated_at, file.updated_by = "failed", utcnow(), None
            await audit.record(
                session,
                "file.upload.failure",
                Actor("system"),
                "file",
                identifier,
                outcome="failure",
                details={"reason": "abandoned_upload"},
            )
        if file.status not in {"deleting", "failed", "deleted"}:
            return
        await require_unreferenced(session, identifier)
        status, storage_key, fingerprint, key = (
            file.status,
            file.storage_key,
            file.storage_fingerprint,
            file.object_key,
        )
    if status in {"deleted", "failed"}:
        # A timed-out PUT may complete after DELETE. Recheck only this registered key;
        # a failed S3 row cannot reactivate, and filesystem retries take the physical lock.
        removed = await blocking(storage.get(storage_key).remove_residue, fingerprint, key)
        async with transaction() as session:
            file = await policy.locked(session, identifier)
            if file.status == status:
                file.residue_checks = min(file.residue_checks + 1, MAX_RESIDUE_CHECKS)
                file.updated_at, file.updated_by = utcnow(), None
                if removed:
                    await audit.record(
                        session, "file.upload.residue_cleanup", Actor("system"), "file", identifier
                    )
        return
    try:
        await blocking(storage.get(storage_key).delete, fingerprint, key)
    except ServiceError as error:
        await _cleanup_failure(transaction, identifier, error, delete_attempt=True)
        return
    async with transaction() as session:
        file = await policy.locked(session, identifier)
        if file.status != "deleting":
            return
        await require_unreferenced(session, identifier)
        file.status, file.deleted_at = "deleted", utcnow()
        file.delete_failures = 0
        file.updated_at, file.updated_by = file.deleted_at, None
        await audit.record(session, "file.delete.complete", Actor("system"), "file", identifier)


async def _cleanup_failure(transaction, identifier, error, *, delete_attempt=False):
    async with transaction() as session:
        file = await policy.locked(session, identifier)
        if delete_attempt or file.status in {"failed", "deleting", "deleted"}:
            # Rotate failed candidates so one broken object cannot starve the batch.
            file.updated_at, file.updated_by = utcnow(), None
        details = {"reason": error.code}
        if delete_attempt:
            file.delete_failures += 1
            details["attempt"] = file.delete_failures
        elif file.status in {"failed", "deleted"}:
            # A failed check still counts, so residue sweeps stay bounded.
            file.residue_checks = min(file.residue_checks + 1, MAX_RESIDUE_CHECKS)
        await audit.record(
            session,
            "file.cleanup.failure",
            Actor("system"),
            "file",
            identifier,
            outcome="failure",
            details=details,
        )

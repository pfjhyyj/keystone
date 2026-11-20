from datetime import timedelta

from sqlalchemy import false, func, select

from keystone.core.errors import ServiceError
from keystone.core.time import utcnow
from keystone.platform.audit.services import writer as audit
from keystone.platform.audit.services.writer import Actor
from keystone.platform.files.adapters.storage import blocking
from keystone.platform.files.models import File, FileReference
from keystone.platform.files.services import access as policy
from keystone.platform.files.services.references import require_unreferenced

MAX_DELETE_FAILURES = 3
DELETE_RETRY_DELAY = timedelta(minutes=1)


def pending_delete():
    return (File.status == "deleting") & (File.delete_failures < MAX_DELETE_FAILURES)


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
    cutoff = utcnow() - timedelta(hours=1)
    async with database.transaction() as session:
        return (
            await session.execute(
                select(func.count(), func.min(File.updated_at)).where(
                    pending_delete()
                    | (File.status.in_(["uploading", "failed"]) & (File.updated_at < cutoff))
                    | expired_unreferenced(unreferenced_grace_seconds)
                )
            )
        ).one()


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
                    | (File.status.in_(["uploading", "failed"]) & (File.updated_at < cutoff))
                    | (
                        (File.status == "deleted")
                        & File.storage_key.in_(remote_keys)
                        & (File.updated_at < cutoff)
                    )
                )
                # Requested deletions go first; recurring residue sweeps cannot starve them.
                .order_by(File.status != "deleting", File.updated_at, File.id)
                .limit(limit)
            )
        )
    for identifier in identifiers:
        try:
            with storage.lease(identifier):
                async with database.transaction() as session:
                    file = await policy.locked(session, identifier)
                    if file.status == "deleting" and (
                        file.delete_failures >= MAX_DELETE_FAILURES
                        or (file.delete_failures and file.updated_at > retry_cutoff)
                    ):
                        continue
                    # Reference creation locks this same row. Recheck after admission,
                    # then commit deleting before performing any external deletion.
                    if file.status == "active":
                        eligible = await session.scalar(
                            select(File.id).where(
                                File.id == identifier,
                                expired_unreferenced(unreferenced_grace_seconds),
                            )
                        )
                        if eligible is None:
                            continue
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
                        continue
                    await require_unreferenced(session, identifier)
                    status, storage_key, fingerprint, key = (
                        file.status,
                        file.storage_key,
                        file.storage_fingerprint,
                        file.object_key,
                    )
                if status in {"deleted", "failed"}:
                    # A timed-out PUT may complete after DELETE. Keep the immutable
                    # tombstone or retryable failure and re-delete its exact registered key,
                    # never list a bucket; only an actual removal is audited.
                    removed = await blocking(
                        storage.get(storage_key).remove_residue, fingerprint, key
                    )
                    async with database.transaction() as session:
                        file = await policy.locked(session, identifier)
                        if file.status == status:
                            file.updated_at, file.updated_by = utcnow(), None
                            if removed:
                                await audit.record(
                                    session,
                                    "file.upload.residue_cleanup",
                                    Actor("system"),
                                    "file",
                                    identifier,
                                )
                    continue
                try:
                    await blocking(storage.get(storage_key).delete, fingerprint, key)
                except ServiceError as error:
                    if error.code != "file_busy":
                        # Persist before releasing the file lease so another pass sees the count.
                        async with database.transaction() as session:
                            file = await policy.locked(session, identifier)
                            file.delete_failures += 1
                            file.updated_at, file.updated_by = utcnow(), None
                            await audit.record(
                                session,
                                "file.cleanup.failure",
                                Actor("system"),
                                "file",
                                identifier,
                                outcome="failure",
                                details={"reason": error.code, "attempt": file.delete_failures},
                            )
                    continue
                async with database.transaction() as session:
                    file = await policy.locked(session, identifier)
                    if file.status != "deleting":
                        continue
                    await require_unreferenced(session, identifier)
                    file.status, file.deleted_at = "deleted", utcnow()
                    file.delete_failures = 0
                    file.updated_at, file.updated_by = file.deleted_at, None
                    await audit.record(
                        session,
                        "file.delete.complete",
                        Actor("system"),
                        "file",
                        identifier,
                    )
        except ServiceError as error:
            if error.code != "file_busy":
                async with database.transaction() as session:
                    file = await policy.locked(session, identifier)
                    if file.status in {"failed", "deleting", "deleted"}:
                        # Rotate failed candidates so one broken object cannot starve the batch.
                        file.updated_at, file.updated_by = utcnow(), None
                    await audit.record(
                        session,
                        "file.cleanup.failure",
                        Actor("system"),
                        "file",
                        identifier,
                        outcome="failure",
                        details={"reason": error.code},
                    )

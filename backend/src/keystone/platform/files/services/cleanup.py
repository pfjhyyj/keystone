from datetime import timedelta

from sqlalchemy import delete, select

from keystone.core.errors import ServiceError
from keystone.core.time import utcnow
from keystone.platform.audit.services import writer as audit
from keystone.platform.audit.services.writer import Actor
from keystone.platform.files import policy
from keystone.platform.files.models import File, FileGrant
from keystone.platform.files.services.references import require_unreferenced
from keystone.platform.files.storage import blocking


async def cleanup(database, storage, *, limit=20):
    """Bounded worker pass. Never delete arbitrary unreferenced active files."""
    cutoff = utcnow() - timedelta(hours=1)
    remote_keys = [key for key, source in storage.sources.items() if source.config.driver == "s3"]
    async with database.transaction() as session:
        identifiers = list(
            await session.scalars(
                select(File.id)
                .where(
                    (File.status == "deleting")
                    | (File.status.in_(["uploading", "failed"]) & (File.updated_at < cutoff))
                    | (
                        (File.status == "deleted")
                        & File.storage_key.in_(remote_keys)
                        & (File.updated_at < cutoff)
                    )
                )
                .order_by(File.updated_at, File.id)
                .limit(limit)
            )
        )
    for identifier in identifiers:
        try:
            with storage.lease(identifier):
                async with database.transaction() as session:
                    file = await policy.locked(session, identifier)
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
                    status, fingerprint, key = (
                        file.status,
                        file.storage_fingerprint,
                        file.object_key,
                    )
                    source = storage.get(file.storage_key)
                if status == "deleted":
                    # A timed-out PUT may complete after DELETE. Keep the immutable
                    # tombstone and re-delete its exact registered key, never list a bucket.
                    removed = await blocking(source.remove_residue, fingerprint, key)
                    async with database.transaction() as session:
                        file = await policy.locked(session, identifier)
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
                await blocking(source.delete, fingerprint, key)
                if status == "failed":
                    async with database.transaction() as session:
                        file = await policy.locked(session, identifier)
                        if file.status == "failed":
                            file.updated_at, file.updated_by = utcnow(), None
                            await audit.record(
                                session,
                                "file.upload.residue_cleanup",
                                Actor("system"),
                                "file",
                                identifier,
                            )
                    continue
                async with database.transaction() as session:
                    file = await policy.locked(session, identifier)
                    if file.status != "deleting":
                        continue
                    await require_unreferenced(session, identifier)
                    result = await session.execute(
                        delete(FileGrant).where(FileGrant.file_id == identifier)
                    )
                    file.status, file.deleted_at = "deleted", utcnow()
                    file.updated_at, file.updated_by = file.deleted_at, None
                    await audit.record(
                        session,
                        "file.delete.complete",
                        Actor("system"),
                        "file",
                        identifier,
                        details={"grants_removed": result.rowcount, "grant_scope": "all_for_file"},
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

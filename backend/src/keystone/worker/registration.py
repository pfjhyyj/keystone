"""Durable fixed identities; membership changes do not change execution configuration."""

from contextlib import asynccontextmanager

from sqlalchemy import select, text

from keystone.core.advisory import lock_key, session_lease
from keystone.core.errors import ServiceError
from keystone.worker.identity import config_digest
from keystone.worker.models import WorkerRegistration

MAX_WORKERS = 4


async def shared_config_matches(session, settings):
    """Compare against active Worker registrations using the caller's transaction."""
    digests = list(
        await session.scalars(
            select(WorkerRegistration.config_digest)
            .where(
                WorkerRegistration.state == "registered",
                WorkerRegistration.stopped_at.is_(None),
                WorkerRegistration.instance_id.is_not(None),
                WorkerRegistration.config_digest.is_not(None),
            )
            .distinct()
        )
    )
    if not digests:
        return True
    expected = config_digest(settings)
    return all(digest == expected for digest in digests)


@asynccontextmanager
async def identity_lease(database, storage, worker_id, *, transaction_timeout=None):
    with storage.lease("worker:" + worker_id):
        async with session_lease(
            database,
            keys=[lock_key("worker-instance:" + worker_id)],
            transaction_timeout=transaction_timeout,
        ) as lease:
            yield lease


async def serialized(session):
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key("worker-registration")}
    )


async def admit(session, worker_id, *, host_id, coordination_id, version, digest, reactivate=False):
    # Caller holds this identity's OS/PG locks, including through commit.
    await serialized(session)
    rows = list(await session.scalars(select(WorkerRegistration).with_for_update()))
    current = next((row for row in rows if row.worker_id == worker_id), None)
    if current and current.state == "retired" and not reactivate:
        raise ServiceError(409, "worker_retired", "身份已退役，请显式重新启用")
    for row in rows:
        if row.state == "retired":
            continue
        if (
            row.host_id is not None
            and row.host_id != host_id
            or row.coordination_id is not None
            and row.coordination_id != coordination_id
            or row.worker_id != worker_id
            and row.stopped_at is None
            and row.instance_id is not None
            and (row.execution_version, row.config_digest) != (version, digest)
        ):
            raise ServiceError(
                409,
                "worker_deployment_conflict",
                "其他 Worker 的主机、版本、协调卷或公共配置不一致",
            )
    if current is None or current.state == "retired":
        if sum(row.state == "registered" for row in rows) >= MAX_WORKERS:
            raise ServiceError(409, "worker_capacity_full", "最多登记四个未退役身份，请先安全退役")
    if current is None:
        current = WorkerRegistration(worker_id=worker_id, state="registered")
        session.add(current)
    current.state, current.retired_at = "registered", None
    current.host_id, current.coordination_id = host_id, coordination_id
    current.execution_version, current.config_digest = version, digest
    return current

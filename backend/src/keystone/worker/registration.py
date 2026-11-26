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


def identity_lease(database, worker_id, *, transaction_timeout=None):
    return session_lease(
        database,
        keys=[lock_key("worker-instance:" + worker_id)],
        transaction_timeout=transaction_timeout,
        busy_error=ServiceError(409, "worker_identity_busy", "此 Worker 身份正在使用"),
        interrupted_error=ServiceError(503, "worker_identity_lost", "Worker 身份锁连接已中断"),
    )


@asynccontextmanager
async def registration_transaction(lease):
    async with lease.transaction() as session:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key("worker-registration")}
        )
        yield session


def conflicts_with_local_storage(row, host_id, coordination_id):
    # A null coordination_id means the database binding was verified for shared storage.
    return coordination_id is not None and (
        (row.host_id is not None and row.host_id != host_id)
        or (row.coordination_id is not None and row.coordination_id != coordination_id)
    )


async def admit(session, worker_id, *, host_id, coordination_id, version, digest, reactivate=False):
    """Admit within a registration_transaction on the identity's leased connection."""
    rows = list(await session.scalars(select(WorkerRegistration).with_for_update()))
    current = next((row for row in rows if row.worker_id == worker_id), None)
    if current and current.state == "retired" and not reactivate:
        raise ServiceError(409, "worker_retired", "身份已退役，请显式重新启用")
    for row in rows:
        if row.state == "retired":
            continue

        incompatible_peer = (
            row.worker_id != worker_id
            and row.stopped_at is None
            and row.instance_id is not None
            and (row.execution_version, row.config_digest) != (version, digest)
        )
        if conflicts_with_local_storage(row, host_id, coordination_id) or incompatible_peer:
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

"""Durable deployment admission for the PostgreSQL database."""

import asyncio
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from hashlib import sha256
from socket import gethostname

from sqlalchemy import CheckConstraint, Column, Integer, String, Table, func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from keystone.core.advisory import lock_key
from keystone.core.config import Settings
from keystone.core.database import Base, Database
from keystone.core.errors import ServiceError
from keystone.core.local_leases import LocalLeases

_BINDING = Table(
    "deployment_binding",
    Base.metadata,
    Column("id", Integer, primary_key=True, autoincrement=False),
    Column("topology", String(16), nullable=False),
    Column("host_id", String(255)),
    Column("coordination_id", String(64)),
    Column("credential_key_fingerprint", String(64)),
    CheckConstraint("id = 1", name="deployment_binding_singleton"),
    CheckConstraint(
        "(topology = 'filesystem' AND host_id IS NOT NULL AND coordination_id IS NOT NULL "
        "AND credential_key_fingerprint IS NULL) OR "
        "(topology = 'shared' AND host_id IS NULL AND coordination_id IS NULL "
        "AND credential_key_fingerprint IS NOT NULL)",
        name="deployment_binding_topology",
    ),
)
_READ_BINDING = select(
    _BINDING.c.topology,
    _BINDING.c.host_id,
    _BINDING.c.coordination_id,
    _BINDING.c.credential_key_fingerprint,
).where(_BINDING.c.id == 1)
_LOCK = lock_key("keystone.deployment.binding")


@dataclass(frozen=True)
class Deployment:
    topology: str
    host_id: str | None = None
    coordination_id: str | None = None
    credential_key_fingerprint: str | None = None

    @classmethod
    def configured(cls, settings: Settings, *, coordination: LocalLeases | None = None):
        if not settings.file_storages:
            raise ServiceError(503, "storage_unavailable", "文件存储未配置")
        coordination = coordination or LocalLeases(settings.file_coordination_root)
        coordination_id = coordination.fingerprint()
        if not settings.s3_only_storage:
            return cls("filesystem", host_id=gethostname()[:255], coordination_id=coordination_id)
        if settings.credential_key is None:
            raise ServiceError(
                503, "credential_key_required", "S3 部署需要 KEYSTONE_CREDENTIAL_KEY"
            )
        key = settings.credential_key.get_secret_value().encode("ascii")
        fingerprint = sha256(b"keystone.deployment.credential\0" + key).hexdigest()
        return cls("shared", credential_key_fingerprint=fingerprint)


async def _read_binding(session: AsyncSession) -> Deployment | None:
    row = (await session.execute(_READ_BINDING)).one_or_none()
    return Deployment(*row) if row is not None else None


async def require_deployment(
    database: Database, settings: Settings, *, coordination: LocalLeases | None = None
) -> Deployment:
    deployment = Deployment.configured(settings, coordination=coordination)
    try:
        async with asyncio.timeout(3), database.transaction() as session:
            stored = await _read_binding(session)
    except (SQLAlchemyError, TimeoutError):
        stored = None
    if stored is None:
        raise ServiceError(503, "deployment_not_bound", "请检查数据库并完成 keystone init")
    _check_binding(stored, deployment)
    return deployment


async def bind_deployment(
    database: Database, deployment: Deployment, *, storage_fingerprints: Mapping[str, str]
) -> None:
    try:
        async with asyncio.timeout(10), database.transaction() as session:
            await session.execute(select(func.pg_advisory_xact_lock(_LOCK)))
            stored = await _read_binding(session)
            if stored is not None:
                _check_binding(stored, deployment)
                return
            if deployment.topology == "filesystem":
                await _check_legacy_host(session, deployment)
            else:
                await _check_legacy_files(session, storage_fingerprints)
            await session.execute(_BINDING.insert().values(id=1, **asdict(deployment)))
    except TimeoutError:
        raise ServiceError(503, "deployment_binding_busy", "部署绑定暂不可用，请稍后重试") from None


def _check_binding(stored: Deployment, expected: Deployment) -> None:
    if stored.topology != expected.topology:
        detail = "数据库已绑定其他文件存储拓扑，不能直接切换文件系统部署与 S3 部署"
    elif stored.host_id != expected.host_id or stored.coordination_id != expected.coordination_id:
        detail = "数据库已绑定其他主机或文件协调目录"
    elif stored.credential_key_fingerprint != expected.credential_key_fingerprint:
        detail = "KEYSTONE_CREDENTIAL_KEY 与数据库部署绑定不一致"
    else:
        return
    raise ServiceError(409, "deployment_binding_conflict", detail)


async def _check_legacy_host(session, deployment: Deployment) -> None:
    # Pre-0011 registrations cannot distinguish local files from S3 with local scratch.
    # Only use their host/root evidence when the operator is claiming filesystem storage.
    conflict = await session.scalar(
        text("""
            SELECT EXISTS (
                SELECT 1 FROM keystone.worker_registrations
                WHERE state = 'registered'
                  AND (host_id != :host_id OR coordination_id != :coordination_id)
            )
        """),
        {"host_id": deployment.host_id, "coordination_id": deployment.coordination_id},
    )
    if conflict:
        raise ServiceError(
            409, "deployment_binding_conflict", "文件系统部署与已有 Worker 的主机或协调目录不一致"
        )


async def _check_legacy_files(session, storage_fingerprints: Mapping[str, str]) -> None:
    locations = await session.execute(
        text("""
            SELECT DISTINCT storage_key, storage_fingerprint
            FROM keystone.files WHERE status <> 'deleted'
        """)
    )
    if any(storage_fingerprints.get(key) != fingerprint for key, fingerprint in locations):
        raise ServiceError(
            409, "deployment_binding_conflict", "现有文件的存储位置与配置的 S3 存储不一致"
        )

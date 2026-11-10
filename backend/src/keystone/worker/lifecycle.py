"""Operator-only fixed-identity retirement; no execution or automatic takeover."""

import asyncio
import re
import socket

from sqlalchemy import func

from keystone.composition import build_catalog, execution_version
from keystone.core.database import Database
from keystone.core.errors import ServiceError
from keystone.core.execution.runtime import has_owned_work
from keystone.core.migrations import expected_heads
from keystone.platform.audit.services import writer as audit
from keystone.platform.files.adapters.storage import StorageRegistry
from keystone.platform.model_access.services.recovery import has_worker_calls
from keystone.worker.identity import config_digest
from keystone.worker.models import WorkerRegistration
from keystone.worker.registration import admit, identity_lease, serialized


async def change_worker(settings, worker_id, *, reactivate=False):
    if not re.fullmatch(r"[a-z][a-z0-9_-]{2,63}", worker_id):
        raise ServiceError(400, "worker_identity_invalid", "固定身份格式不合法")
    database = Database(settings, catalog=build_catalog())
    try:
        checks = await database.readiness(expected_heads(settings))
        if any(value != "ok" for value in checks.values()):
            raise ServiceError(503, "database_not_ready", "请先运行 keystone init")
        storage = StorageRegistry(settings)
        coordination_id = storage.coordination.fingerprint()
        host_id = socket.gethostname()[:255]
        async with asyncio.timeout(10), identity_lease(database, storage, worker_id) as lease:
            async with lease.transaction() as session:
                await serialized(session)
                row = await session.get(WorkerRegistration, worker_id, with_for_update=True)
                if row is None:
                    raise ServiceError(404, "worker_not_registered", "固定身份尚未登记")
                if (
                    row.host_id is not None
                    and row.host_id != host_id
                    or row.coordination_id is not None
                    and row.coordination_id != coordination_id
                ):
                    raise ServiceError(
                        409, "worker_deployment_conflict", "身份的主机或协调卷不一致"
                    )
                desired = "registered" if reactivate else "retired"
                if row.state == desired:
                    return
                if reactivate:
                    row = await admit(
                        session,
                        worker_id,
                        host_id=host_id,
                        coordination_id=coordination_id,
                        version=execution_version(),
                        digest=config_digest(settings),
                        reactivate=True,
                    )
                    row.instance_id, row.stopped_at = None, func.clock_timestamp()
                else:
                    if await has_owned_work(settings, worker_id) or await has_worker_calls(
                        session, worker_id
                    ):
                        raise ServiceError(
                            409,
                            "worker_recovery_required",
                            "身份仍有待恢复执行或模型调用，拒绝退役",
                        )
                    row.state, row.retired_at = "retired", func.clock_timestamp()
                await audit.record(
                    session,
                    "worker.reactivate" if reactivate else "worker.retire",
                    audit.Actor("system"),
                    "worker",
                    worker_id,
                    details={"reason": "operator_request"},
                )
    finally:
        await database.close()

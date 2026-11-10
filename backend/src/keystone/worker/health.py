"""Always-on Worker observations, shared through the existing PostgreSQL database."""

import asyncio
import socket
import time
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert

from keystone.worker.identity import WorkerIdentity, config_digest
from keystone.worker.models import WorkerHeartbeat, WorkerRegistration
from keystone.worker.registration import admit

MAX_AGE_SECONDS = 30
IO_TIMEOUT_SECONDS = 3


class WorkerHealth:
    def __init__(self, database, settings=None, application_version=None):
        self.database = database
        self.settings = settings
        self.instance_id = uuid4()
        self.host_id = socket.gethostname()[:255]
        self.worker_id = settings.worker_id if settings else "keystone-worker"
        self.application_version = application_version
        self.digest = config_digest(settings) if settings else None
        self.coordination_id = None
        self.identity = WorkerIdentity(self.worker_id, self.instance_id, application_version)
        self.claimed = settings is None
        self.task_results = {}
        self.published_checks = {}
        self.state = "starting"
        self.checked_at = None
        self.task_checks = {}
        self.publish_lock = asyncio.Lock()

    async def claim(self, coordination_id, lease):
        # All admission writes commit on the connection that holds this identity's lock.
        self.coordination_id = coordination_id
        async with lease.transaction() as session:
            registration = await admit(
                session,
                self.worker_id,
                host_id=self.host_id,
                coordination_id=coordination_id,
                version=self.application_version,
                digest=self.digest,
            )
            registration.instance_id, registration.stopped_at = self.instance_id, None
            await session.execute(
                update(WorkerHeartbeat)
                .where(WorkerHeartbeat.worker_id == self.worker_id)
                .values(state="stopped")
            )
            session.add(
                WorkerHeartbeat(
                    instance_id=self.instance_id,
                    worker_id=self.worker_id,
                    host_id=self.host_id,
                    execution_version=self.application_version,
                    config_digest=self.digest,
                    coordination_id=self.coordination_id,
                    state="starting",
                    heartbeat_at=func.clock_timestamp(),
                    task_results={},
                )
            )
        self.claimed = True

    def checked(self, healthy):
        if self.state in {"draining", "stopped"}:
            return
        self.state = "ready" if healthy else "unready"
        self.checked_at = time.monotonic()

    def require_task(self, name):
        self.task_checks[name] = (False, None)

    def checked_task(self, name, healthy):
        self.task_checks[name] = (healthy, time.monotonic())

    def observation(self, executor_ready=True):
        if self.state == "ready" and (
            not executor_ready
            or self.checked_at is None
            or time.monotonic() - self.checked_at > MAX_AGE_SECONDS
        ):
            return "unready"
        return self.state

    async def publish(self, state):
        if not self.claimed:
            return
        async with (
            self.publish_lock,
            asyncio.timeout(IO_TIMEOUT_SECONDS),
            self.database.transaction() as session,
        ):
            now = await session.scalar(select(func.clock_timestamp()))
            # Admission locks registration before heartbeat rows. Keep the same order
            # when the old process reports its final stop after releasing its identity.
            if state == "stopped":
                await session.execute(
                    update(WorkerRegistration)
                    .where(
                        WorkerRegistration.worker_id == self.worker_id,
                        WorkerRegistration.instance_id == self.instance_id,
                        WorkerRegistration.state == "registered",
                    )
                    .values(stopped_at=func.clock_timestamp())
                )
            pending = dict(self.task_checks)
            results = dict(self.task_results)
            for name, check in pending.items():
                if check[1] is not None and check != self.published_checks.get(name):
                    completed_at = now - timedelta(seconds=max(0, time.monotonic() - check[1]))
                    results[name] = {"healthy": check[0], "checked_at": completed_at.isoformat()}
            await session.execute(
                insert(WorkerHeartbeat)
                .values(
                    instance_id=self.instance_id,
                    host_id=self.host_id,
                    worker_id=self.worker_id,
                    execution_version=self.application_version,
                    config_digest=self.digest,
                    coordination_id=self.coordination_id,
                    task_results=results,
                    state=state,
                    heartbeat_at=func.clock_timestamp(),
                )
                .on_conflict_do_update(
                    index_elements=[WorkerHeartbeat.instance_id],
                    set_={
                        "state": state,
                        "heartbeat_at": func.clock_timestamp(),
                        "task_results": results,
                    },
                    where=(WorkerHeartbeat.state != "stopped")
                    & (
                        (WorkerHeartbeat.state != "draining")
                        if state not in {"draining", "stopped"}
                        else True
                    ),
                )
            )
            await session.execute(
                delete(WorkerHeartbeat).where(
                    WorkerHeartbeat.heartbeat_at < func.clock_timestamp() - timedelta(days=1),
                    WorkerHeartbeat.state == "stopped",
                )
            )
        self.task_results, self.published_checks = results, pending


async def worker_status(
    database,
    *,
    host_id=None,
    worker_id=None,
    settings=None,
    application_version=None,
    process_only=False,
):
    try:
        async with asyncio.timeout(IO_TIMEOUT_SECONDS), database.transaction() as session:
            now = await session.scalar(select(func.clock_timestamp()))
            query = select(WorkerHeartbeat).order_by(
                WorkerHeartbeat.started_at.desc(), WorkerHeartbeat.instance_id
            )
            if host_id is not None:
                query = query.where(WorkerHeartbeat.host_id == host_id)
            if worker_id is not None:
                query = query.where(WorkerHeartbeat.worker_id == worker_id)
            rows = list(await session.scalars(query))
            registration_query = select(WorkerRegistration).order_by(WorkerRegistration.worker_id)
            if worker_id is not None:
                registration_query = registration_query.where(
                    WorkerRegistration.worker_id == worker_id
                )
            registrations = list(await session.scalars(registration_query))
        latest = {}
        for row in rows:
            latest.setdefault(row.worker_id, row)
        registered = {r.worker_id: r for r in registrations}
        expected = [r.worker_id for r in registrations if r.state == "registered"]
        by_instance = {r.instance_id: r for r in rows}
        instances = []
        for identifier in dict.fromkeys([*registered, *latest]):
            member = registered.get(identifier)
            row = by_instance.get(member.instance_id) if member else latest.get(identifier)
            if member and member.state == "retired":
                instances.append(
                    {
                        "worker_id": identifier,
                        "instance_id": str(member.instance_id) if member.instance_id else None,
                        "status": "offline",
                        "state": "retired",
                        "execution_version": member.execution_version,
                        "last_heartbeat_at": row.heartbeat_at.isoformat() if row else None,
                        "expected": False,
                    }
                )
                continue
            if row is None:
                instances.append(
                    {
                        "worker_id": identifier,
                        "instance_id": None,
                        "status": "offline",
                        "state": "missing",
                        "execution_version": None,
                        "last_heartbeat_at": None,
                        "expected": identifier in expected,
                    }
                )
                continue
            age = (now - row.heartbeat_at).total_seconds()
            status = (
                "offline"
                if row.state == "stopped" or not 0 <= age <= MAX_AGE_SECONDS
                else "ready"
                if row.state == "ready"
                else "unready"
            )
            if (
                application_version is not None
                and row.execution_version != application_version
                and status == "ready"
            ):
                status = "unready"
            if settings and row.config_digest != config_digest(settings) and status == "ready":
                status = "unready"
            instances.append(
                {
                    "worker_id": identifier,
                    "instance_id": str(row.instance_id),
                    "status": status,
                    "state": row.state,
                    "execution_version": row.execution_version,
                    "last_heartbeat_at": row.heartbeat_at,
                    "expected": identifier in expected,
                }
            )
        maintenance = {}
        if settings and not process_only:
            from datetime import datetime

            required = [
                "model_costs",
                *(["files"] if settings.file_storages else []),
                "flow",
            ]
            for name in required:
                checks = [
                    r.task_results[name]
                    for r in rows
                    if r.worker_id in expected
                    and r.execution_version == application_version
                    and r.config_digest == config_digest(settings)
                    and name in r.task_results
                ]
                result = max(
                    checks, key=lambda c: datetime.fromisoformat(c["checked_at"]), default=None
                )
                healthy = bool(
                    result
                    and result["healthy"]
                    and 0
                    <= (now - datetime.fromisoformat(result["checked_at"])).total_seconds()
                    <= MAX_AGE_SECONDS
                )
                maintenance[name] = {
                    "status": "ready" if healthy else "unready",
                    "last_checked_at": result["checked_at"] if result else None,
                }
        current = [row for row in instances if row["expected"]]
        ready = sum(row["status"] == "ready" for row in current)
        status = (
            "ready"
            if current
            and ready == len(current)
            and all(c["status"] == "ready" for c in maintenance.values())
            else "offline"
            if all(row["status"] == "offline" for row in current)
            else "unready"
        )
        if any(not row["expected"] and row["status"] != "offline" for row in instances):
            status = "unready"
        return {
            "status": status,
            "last_heartbeat_at": max(
                (r["last_heartbeat_at"] for r in current if r["last_heartbeat_at"] is not None),
                default=None,
            ),
            "instances": instances,
            "maintenance": maintenance,
            "ready_workers": ready,
            "expected_workers": len(current),
        }
    except Exception:
        return {
            "status": "unknown",
            "last_heartbeat_at": None,
            "instances": [],
            "maintenance": {},
            "ready_workers": 0,
            "expected_workers": 0,
        }

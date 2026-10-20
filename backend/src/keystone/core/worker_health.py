"""Always-on Worker observations, shared through the existing PostgreSQL database."""

import asyncio
import socket
import time
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import case, delete, func, select
from sqlalchemy.dialects.postgresql import insert

from keystone.core.models import WorkerHeartbeat

MAX_AGE_SECONDS = 30
IO_TIMEOUT_SECONDS = 3


class WorkerHealth:
    def __init__(self, database):
        self.database = database
        self.instance_id = uuid4()
        self.host_id = socket.gethostname()[:255]
        self.state = "starting"
        self.checked_at = None

    def checked(self, healthy):
        self.state = "ready" if healthy else "unready"
        self.checked_at = time.monotonic()

    def observation(self, executor_ready=True):
        if self.state == "ready" and (
            not executor_ready
            or self.checked_at is None
            or time.monotonic() - self.checked_at > MAX_AGE_SECONDS
        ):
            return "unready"
        return self.state

    async def publish(self, state):
        async with asyncio.timeout(IO_TIMEOUT_SECONDS), self.database.transaction() as session:
            await session.execute(
                insert(WorkerHeartbeat)
                .values(
                    instance_id=self.instance_id,
                    host_id=self.host_id,
                    state=state,
                    heartbeat_at=func.clock_timestamp(),
                )
                .on_conflict_do_update(
                    index_elements=[WorkerHeartbeat.instance_id],
                    set_={"state": state, "heartbeat_at": func.clock_timestamp()},
                )
            )
            await session.execute(
                delete(WorkerHeartbeat).where(
                    WorkerHeartbeat.heartbeat_at < func.clock_timestamp() - timedelta(days=1)
                )
            )


async def worker_status(database, *, host_id=None):
    try:
        async with asyncio.timeout(IO_TIMEOUT_SECONDS), database.transaction() as session:
            fresh = (
                (WorkerHeartbeat.state != "stopped")
                & (
                    WorkerHeartbeat.heartbeat_at
                    >= func.clock_timestamp() - timedelta(seconds=MAX_AGE_SECONDS)
                )
                & (WorkerHeartbeat.heartbeat_at <= func.clock_timestamp())
            )
            query = select(WorkerHeartbeat, func.clock_timestamp()).order_by(
                case((fresh, 0), else_=1),
                case((fresh & (WorkerHeartbeat.state == "ready"), 0), else_=1),
                WorkerHeartbeat.heartbeat_at.desc(),
            )
            if host_id is not None:
                query = query.where(WorkerHeartbeat.host_id == host_id)
            result = (await session.execute(query.limit(1))).first()
            if result is None:
                return {"status": "offline", "last_heartbeat_at": None}
            row, now = result
            age = (now - row.heartbeat_at).total_seconds()
            status = (
                "offline"
                if row.state == "stopped" or not 0 <= age <= MAX_AGE_SECONDS
                else "ready"
                if row.state == "ready"
                else "unready"
            )
            return {"status": status, "last_heartbeat_at": row.heartbeat_at}
    except Exception:
        return {"status": "unknown", "last_heartbeat_at": None}

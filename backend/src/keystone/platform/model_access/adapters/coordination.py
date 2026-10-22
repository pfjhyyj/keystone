"""Database coordination and process budgets, independent of HTTP and suppliers."""

import asyncio
from contextlib import asynccontextmanager

from sqlalchemy import select

from keystone.core.advisory import session_lease
from keystone.core.errors import ServiceError
from keystone.core.models import WorkerRegistration
from keystone.core.worker_identity import config_digest


class QueryCoordination:
    def __init__(self, database, settings=None):
        self.database, self.settings = database, settings
        self.cost_slots = asyncio.Semaphore(2)
        self.balance_slots = asyncio.Semaphore(2)

    def scope(self, identifier):
        return refresh_lock(self.database, identifier)

    @asynccontextmanager
    async def cost_scope(self, identifier):
        count = self.settings.model_cost_concurrency if self.settings else 2
        async with session_lease(
            self.database,
            keys=[int.from_bytes(identifier.bytes[:8], signed=True)],
            capacities=[("model-cost", count)],
        ) as lease:
            await self.check_config(lease)
            yield lease

    @asynccontextmanager
    async def inference(self, scope):
        total = self.settings.model_inference_concurrency if self.settings else 4
        per_scope = self.settings.model_scope_concurrency if self.settings else 2
        async with session_lease(
            self.database,
            capacities=[("model-inference", total), ("model-inference:" + scope, per_scope)],
        ) as lease:
            await self.check_config(lease)
            yield lease

    async def check_config(self, lease):
        if self.settings is not None:
            async with lease.transaction() as session:
                digests = await session.scalars(
                    select(WorkerRegistration.config_digest)
                    .where(
                        WorkerRegistration.state == "registered",
                        WorkerRegistration.stopped_at.is_(None),
                        WorkerRegistration.instance_id.is_not(None),
                        WorkerRegistration.config_digest.is_not(None),
                    )
                    .distinct()
                )
                if any(digest != config_digest(self.settings) for digest in digests):
                    raise ServiceError(
                        409, "worker_deployment_conflict", "模型请求的公共配置与 Worker 不一致"
                    )


@asynccontextmanager
async def refresh_lock(database, identifier):
    """Session advisory lock spans external I/O, without an open business transaction."""
    async with session_lease(
        database, keys=[int.from_bytes(identifier.bytes[:8], signed=True)]
    ) as lease:
        yield lease

"""DBOS lifecycle for each fixed Worker, independent of business Flow."""

import asyncio
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from dbos import DBOS, DBOSClient

from keystone.core.config import Settings
from keystone.core.errors import ServiceError


def _workflow_id():
    return DBOS.workflow_id


@asynccontextmanager
async def lifespan(
    settings: Settings, *, application_version: str, queues: Mapping[str, tuple[int, int]], lease
):
    # Startup recovery can dequeue immediately. Persist all budgets before any executor launches.
    client = DBOSClient(
        system_database_url=settings.database_url.get_secret_value(),
        application_name="keystone",
        lazy=True,
        retry_connection_errors=False,
    )
    try:
        for name, (local, total) in queues.items():
            await client.register_queue_async(
                name, worker_concurrency=local, global_concurrency=total
            )
    finally:
        await asyncio.to_thread(client.destroy)
    # Queue setup awaits on a separate connection. Recheck identity before DBOS can
    # immediately recover workflows or consume queued work on launch.
    async with asyncio.timeout(3):
        await lease()
    DBOS(
        config={
            "name": "keystone",
            "application_version": application_version,
            "executor_id": settings.worker_id,
            "system_database_url": settings.database_url.get_secret_value(),
            "dbos_system_schema": "dbos",
            "run_migrations": False,
            "enable_otlp": False,
            "run_admin_server": False,
            "log_level": "CRITICAL",
            "sys_db_pool_size": 5,
            "max_executor_threads": 32,
            "scheduler_polling_interval_sec": 1,
            "db_engine_kwargs": {
                "connect_args": {"connect_timeout": 3},
                "hide_parameters": True,
            },
        }
    )
    try:
        # Persisted queues outlive a process; disabled capabilities must not consume them.
        DBOS.listen_queues(list(queues))
        DBOS.launch()
        yield
    finally:
        # Stop polling and allow in-flight work to finish before releasing the Worker lock.
        try:
            await asyncio.to_thread(
                DBOS.destroy, workflow_completion_timeout_sec=settings.worker_shutdown_seconds
            )
        finally:
            # DBOS adopts this loop and closes its default executor on destroy.
            asyncio.get_running_loop().set_default_executor(
                ThreadPoolExecutor(max_workers=4, thread_name_prefix="keystone-cleanup")
            )
            # Python 3.12 exposes each task's context. Only cancel tasks still executing
            # a DBOS workflow; unrelated tasks created after launch belong to their callers.
            pending = {
                task
                for task in asyncio.all_tasks()
                if task is not asyncio.current_task()
                and task.get_context().run(_workflow_id) is not None
            }
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)


async def inspect_queues(settings):
    """Bounded read-only deployment diagnostics, without inputs, outputs or an executor."""
    client = None
    try:
        client = DBOSClient(
            system_database_url=settings.database_url.get_secret_value(),
            application_name="keystone",
            lazy=True,
            retry_connection_errors=False,
            observability_query_timeout_sec=3,
        )
        rows = await client.list_workflows_async(
            status=["PENDING", "ENQUEUED", "DELAYED"],
            load_input=False,
            load_output=False,
            limit=1000,
        )
        groups = {}
        for row in rows:
            key = (row.queue_name, row.executor_id, row.app_version, row.status)
            groups[key] = groups.get(key, 0) + 1
        return {
            "status": "ok",
            "truncated": len(rows) == 1000,
            "active": [
                {
                    "queue": key[0],
                    "worker_id": key[1],
                    "execution_version": key[2],
                    "state": key[3],
                    "count": count,
                }
                for key, count in groups.items()
            ],
        }
    except Exception:
        return {"status": "unknown", "truncated": False, "active": []}
    finally:
        if client is not None:
            await asyncio.to_thread(client.destroy)


async def has_owned_work(settings, worker_id):
    """Exact owner-filtered existence query, never a truncated aggregate or an executor."""
    client = None
    try:
        client = DBOSClient(
            system_database_url=settings.database_url.get_secret_value(),
            application_name="keystone",
            lazy=True,
            retry_connection_errors=False,
            observability_query_timeout_sec=3,
        )
        return bool(
            await client.list_workflows_async(
                status=["PENDING", "ENQUEUED", "DELAYED"],
                executor_id=worker_id,
                load_input=False,
                load_output=False,
                limit=1,
            )
        )
    except Exception:
        raise ServiceError(
            503, "worker_inspection_unavailable", "无法确认身份执行是否清空，拒绝退役"
        ) from None
    finally:
        if client is not None:
            await asyncio.to_thread(client.destroy)

"""Single executor, fixed identity and public DBOS APIs; no custom lease/queue tables."""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

from dbos import DBOS

from keystone.platform.flow import host
from keystone.platform.flow.catalog import execution_version
from keystone.platform.flow.engine import QUEUE
from keystone.platform.flow.reconcile import reconcile
from keystone.platform.model_access.services.recovery import recover_interrupted_calls

log = logging.getLogger(__name__)


async def run(runtime, stop, *, ready=None):
    if (await runtime.readiness())["status"] != "ready":
        raise RuntimeError("Run migrations and initialize storage before starting Flow Worker")
    # The shared local file volume is already required. OS lock survives no dead connection,
    # and prevents overlapping old/new Worker processes on this supported single-host deployment.
    with runtime.storage.lease("keystone-flow-worker"):
        existing = asyncio.all_tasks()
        host.bind(runtime)
        DBOS(
            config={
                "name": "keystone",
                "application_version": execution_version(),
                "executor_id": "keystone-worker",
                "system_database_url": runtime.settings.database_url.get_secret_value(),
                "dbos_system_schema": "dbos",
                "run_migrations": False,
                "enable_otlp": False,
                "run_admin_server": False,
                "log_level": "CRITICAL",
                "sys_db_pool_size": 5,
                "db_engine_kwargs": {
                    "connect_args": {"connect_timeout": 3},
                    "hide_parameters": True,
                },
            }
        )
        try:
            await recover_interrupted_calls(runtime.database)
            DBOS.launch()
            await DBOS.register_queue_async(
                QUEUE, worker_concurrency=runtime.settings.flow_worker_concurrency
            )
            if ready is not None:
                ready.set()
            log.info("flow_worker_ready executor=keystone-worker version=%s", execution_version())
            cursor = None
            while not stop.is_set():
                cursor = await reconcile(runtime, runtime.flow_engine, cursor)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=5)
                except TimeoutError:
                    pass
        finally:
            if ready is not None:
                ready.clear()
            # Ether shutdown order: stop DBOS polling, drain, cancel remaining adopted-loop work,
            # then release platform clients and the executor lock. Never mark all runs failed.
            await asyncio.to_thread(DBOS.destroy, workflow_completion_timeout_sec=5)
            # DBOS adopts this loop and closes its default executor on destroy.
            # Give remaining client/file cleanup a live pool; asyncio.run owns its shutdown.
            asyncio.get_running_loop().set_default_executor(
                ThreadPoolExecutor(max_workers=4, thread_name_prefix="keystone-cleanup")
            )
            pending = asyncio.all_tasks() - existing
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            host.bind(None)

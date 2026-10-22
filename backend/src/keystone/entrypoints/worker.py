import argparse
import asyncio
import json
import logging
import signal
import socket
from contextlib import AsyncExitStack

from keystone.bootstrap import build_runtime
from keystone.composition import (
    build_catalog,
    execution_version,
    register_maintenance,
    register_workflows,
)
from keystone.core.asyncio import run as run_async
from keystone.core.config import Settings
from keystone.core.database import Database
from keystone.core.errors import ServiceError
from keystone.core.execution.runtime import inspect_queues
from keystone.core.execution.runtime import lifespan as dbos_lifespan
from keystone.core.logging import configure_logging, safe_exception
from keystone.core.worker_health import WorkerHealth, worker_status
from keystone.core.worker_registration import identity_lease

log = logging.getLogger(__name__)
POLL_SECONDS = 5
HEARTBEAT_SECONDS = 5
LEASE_CHECK_SECONDS = 3


async def report_health(health, stop, executor_ready):
    failed = False
    while not stop.is_set():
        try:
            await health.publish(health.observation(executor_ready()))
            if failed:
                log.info("worker_heartbeat_recovered")
            failed = False
        except Exception as error:
            if not failed:
                log.error(
                    "worker_heartbeat_failed type=%s",
                    type(error).__name__,
                    extra={"exception": safe_exception(error)},
                )
            failed = True
        try:
            await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_SECONDS)
        except TimeoutError:
            pass


async def probe(settings):
    database = Database(settings, catalog=build_catalog())
    try:
        return (
            await worker_status(
                database,
                host_id=socket.gethostname()[:255],
                worker_id=settings.worker_id,
                settings=settings,
                application_version=execution_version(),
                process_only=True,
            )
        )["status"] == "ready"
    finally:
        await database.close()


async def run_executor(runtime, stop, *, ready, health):
    if (await runtime.readiness())["status"] != "ready":
        raise RuntimeError("Run migrations and initialize storage before starting Worker")
    async with identity_lease(
        runtime.database,
        runtime.storage,
        runtime.settings.worker_id,
        transaction_timeout=LEASE_CHECK_SECONDS,
    ) as lease:
        async with asyncio.timeout(LEASE_CHECK_SECONDS):
            await health.claim(runtime.storage.coordination.fingerprint(), lease)
        runtime.worker_identity = health.identity
        async with AsyncExitStack() as stack:
            queues, installers = register_maintenance(runtime, health)
            if runtime.settings.flow_enabled:
                from keystone.platform.flow.workflows import bootstrap as flow_worker

                register_workflows()
                await stack.enter_async_context(flow_worker.prepare(runtime, lease=lease))
                queues[flow_worker.QUEUE] = (
                    runtime.settings.flow_worker_concurrency,
                    runtime.settings.flow_global_concurrency,
                )
            if stop.is_set():
                return
            await stack.enter_async_context(
                dbos_lifespan(
                    runtime.settings,
                    application_version=execution_version(),
                    queues=queues,
                    lease=lease,
                )
            )
            # Recovery has finished using the identity connection. Keep watching
            # ownership while schedules install; lease transactions serialize checks.
            runtime.worker_lease = lease
            try:
                if runtime.settings.flow_enabled:
                    flow_worker.report_ready()
                for install in installers:
                    await install()
                async with asyncio.timeout(LEASE_CHECK_SECONDS):
                    await lease()
                if stop.is_set():
                    return
                ready.set()
                await stop.wait()
            finally:
                ready.clear()
                runtime.worker_lease = None
                health.state = "draining"
                await health.publish("draining")


async def run(settings: Settings, stop: asyncio.Event) -> None:
    runtime = build_runtime(settings)
    health = WorkerHealth(runtime.database, settings, execution_version())
    previous = None
    log.info("worker_started flow_enabled=%s", settings.flow_enabled)
    executor_task = None
    executor_ready = asyncio.Event()
    heartbeat_task = None
    heartbeat_stop = asyncio.Event()
    try:
        executor_task = asyncio.create_task(
            run_executor(runtime, stop, ready=executor_ready, health=health)
        )
        heartbeat_task = asyncio.create_task(
            report_health(
                health,
                heartbeat_stop,
                lambda: executor_ready.is_set() and not executor_task.done(),
            )
        )
        while not stop.is_set():
            if executor_task.done():
                executor_task.result()
                break
            state = await runtime.readiness()
            if getattr(runtime, "worker_lease", None) is not None:
                async with asyncio.timeout(LEASE_CHECK_SECONDS):
                    await runtime.worker_lease()
            if state != previous:
                log.info(
                    "worker_dependencies status=%s checks=%s", state["status"], state["checks"]
                )
                previous = state
            healthy = state["status"] == "ready"
            healthy = healthy and executor_ready.is_set() and not executor_task.done()
            health.checked(healthy and not stop.is_set())
            try:
                await asyncio.wait_for(stop.wait(), timeout=POLL_SECONDS)
            except TimeoutError:
                pass
    finally:
        stop.set()
        try:
            try:
                if executor_task is not None:
                    await executor_task
            finally:
                heartbeat_stop.set()
                if heartbeat_task is not None:
                    await asyncio.gather(heartbeat_task, return_exceptions=True)
                try:
                    await health.publish("stopped")
                except Exception:
                    log.warning("worker_stop_heartbeat_failed")
        finally:
            try:
                if settings.flow_enabled and runtime.flow_engine:
                    await runtime.flow_engine.close()
            finally:
                await runtime.database.close()
        log.info("worker_stopped")


async def serve(settings: Settings) -> None:
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    signals = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGBREAK"):
        signals.append(signal.SIGBREAK)
    previous = {}
    try:
        for sig in signals:
            previous[sig] = signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
        await run(settings, stop)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Keystone Worker (DBOS runtime, optional business Flow and file cleanup)"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check this fixed Worker's local process heartbeat without starting",
    )
    parser.add_argument(
        "--inspect",
        action="store_true",
        help="Print fixed Worker and active queue diagnostics without starting an executor",
    )
    args = parser.parse_args()
    try:
        settings = Settings()
    except Exception as error:
        log.error(
            "configuration_failed type=%s",
            type(error).__name__,
            extra={"exception": safe_exception(error)},
        )
        raise SystemExit(1) from None
    configure_logging(settings.log_level)
    if args.inspect:

        async def inspect():
            database = Database(settings, catalog=build_catalog())
            try:
                return {
                    "workers": await worker_status(
                        database, settings=settings, application_version=execution_version()
                    ),
                    "queues": await inspect_queues(settings),
                }
            finally:
                await database.close()

        print(json.dumps(run_async(inspect()), default=str, ensure_ascii=False))
        return
    if args.check:
        healthy = run_async(probe(settings))
        print("Worker healthy" if healthy else "Worker unavailable")
        raise SystemExit(0 if healthy else 1)
    try:
        run_async(serve(settings))
    except Exception as error:
        log.error(
            "worker_failed code=%s type=%s",
            error.code if isinstance(error, ServiceError) else "worker_failed",
            type(error).__name__,
        )
        raise SystemExit(1) from None

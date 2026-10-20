import argparse
import asyncio
import logging
import signal
import socket

from keystone.bootstrap import build_runtime
from keystone.composition import build_catalog, register_workflows
from keystone.core.asyncio import run as run_async
from keystone.core.config import Settings
from keystone.core.database import Database
from keystone.core.logging import configure_logging, safe_exception
from keystone.core.worker_health import WorkerHealth, worker_status
from keystone.platform.files.services.cleanup import cleanup

log = logging.getLogger(__name__)
POLL_SECONDS = 5
HEARTBEAT_SECONDS = 5


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
        return (await worker_status(database, host_id=socket.gethostname()[:255]))[
            "status"
        ] == "ready"
    finally:
        await database.close()


async def run(settings: Settings, stop: asyncio.Event) -> None:
    runtime = build_runtime(settings)
    health = WorkerHealth(runtime.database)
    previous = None
    log.info("worker_started flow_enabled=%s", settings.flow_enabled)
    flow_task = None
    flow_ready = asyncio.Event()
    heartbeat_task = None
    try:
        if settings.flow_enabled:
            from keystone.platform.flow.worker import run as run_flow

            register_workflows()
            flow_task = asyncio.create_task(run_flow(runtime, stop, ready=flow_ready))
        heartbeat_task = asyncio.create_task(
            report_health(
                health,
                stop,
                lambda: (
                    not settings.flow_enabled
                    or (flow_ready.is_set() and flow_task is not None and not flow_task.done())
                ),
            )
        )
        while not stop.is_set():
            if flow_task and flow_task.done():
                flow_task.result()
                break
            state = await runtime.readiness()
            if state != previous:
                log.info(
                    "worker_dependencies status=%s checks=%s", state["status"], state["checks"]
                )
                previous = state
            healthy = state["status"] == "ready"
            # A failed source must not prevent cleanup of other sources or its own retries.
            database_ready = all(state["checks"].get(key) == "ok" for key in ("database", "schema"))
            if (healthy or database_ready) and settings.file_storages:
                try:
                    await cleanup(runtime.database, runtime.storage)
                except Exception as error:
                    healthy = False
                    log.error(
                        "file_cleanup_failed type=%s",
                        type(error).__name__,
                        extra={"exception": safe_exception(error)},
                    )
            if flow_task:
                healthy = healthy and flow_ready.is_set() and not flow_task.done()
            health.checked(healthy and not stop.is_set())
            try:
                await asyncio.wait_for(stop.wait(), timeout=POLL_SECONDS)
            except TimeoutError:
                pass
    finally:
        stop.set()
        try:
            try:
                if heartbeat_task is not None:
                    await asyncio.gather(heartbeat_task, return_exceptions=True)
                try:
                    await health.publish("stopped")
                except Exception:
                    log.warning("worker_stop_heartbeat_failed")
            finally:
                if flow_task:
                    await flow_task
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
        description="Keystone Worker (file cleanup and optional DBOS Flow executor)"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check this host's recent Worker heartbeat without starting",
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
    if args.check:
        healthy = run_async(probe(settings))
        print("Worker healthy" if healthy else "Worker unavailable")
        raise SystemExit(0 if healthy else 1)
    run_async(serve(settings))

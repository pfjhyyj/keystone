import argparse
import asyncio
import logging
import signal

from keystone.bootstrap import build_runtime, register_workflows
from keystone.core.asyncio import run as run_async
from keystone.core.config import Settings
from keystone.core.logging import configure_logging
from keystone.core.worker_health import WorkerHealth
from keystone.platform.files.services.cleanup import cleanup

log = logging.getLogger(__name__)
POLL_SECONDS = 5


async def run(settings: Settings, stop: asyncio.Event) -> None:
    health = WorkerHealth(settings.worker_ready_file)
    health.clear()
    runtime = build_runtime(settings)
    previous = None
    log.info("worker_started flow_enabled=%s", settings.flow_enabled)
    flow_task = None
    flow_ready = asyncio.Event()
    try:
        if settings.flow_enabled:
            from keystone.platform.flow.worker import run as run_flow

            register_workflows()
            flow_task = asyncio.create_task(run_flow(runtime, stop, ready=flow_ready))
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
                    log.error("file_cleanup_failed type=%s", type(error).__name__)
            if flow_task:
                healthy = healthy and flow_ready.is_set() and not flow_task.done()
            if healthy and not stop.is_set():
                health.refresh()
            else:
                health.clear()
            try:
                await asyncio.wait_for(stop.wait(), timeout=POLL_SECONDS)
            except TimeoutError:
                pass
    finally:
        stop.set()
        health.clear()
        try:
            if flow_task:
                await flow_task
        finally:
            if settings.flow_enabled and runtime.flow_engine:
                await runtime.flow_engine.close()
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
        "--check", action="store_true", help="Check the local Worker ready file without starting"
    )
    args = parser.parse_args()
    try:
        settings = Settings()
    except Exception as error:
        log.error("configuration_failed type=%s", type(error).__name__)
        raise SystemExit(1) from None
    configure_logging(settings.log_level)
    if args.check:
        healthy = WorkerHealth(settings.worker_ready_file).healthy()
        print("Worker healthy" if healthy else "Worker unavailable")
        raise SystemExit(0 if healthy else 1)
    run_async(serve(settings))

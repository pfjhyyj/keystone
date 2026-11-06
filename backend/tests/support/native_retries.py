"""Run the actual DBOS retry/checkpoint/cancellation boundary in an isolated process."""

import asyncio
import os
from uuid import uuid4

import psycopg
from dbos import DBOS, SetWorkflowID
from dbos import error as dbos_error

from keystone.core.asyncio import run
from keystone.core.errors import ServiceError
from keystone.core.execution.steps import safe_step
from keystone.core.retries import RetryPolicy

url = os.environ["KEYSTONE_NATIVE_RETRY_TEST_URL"]
sql_url = url.replace("postgresql+psycopg://", "postgresql://", 1)


@safe_step(name="test.retry.persist", idempotent=True, retry_policy=RetryPolicy())
async def persist():
    with psycopg.connect(sql_url) as connection:
        attempt = connection.execute(
            "UPDATE retry_probe SET attempts=attempts+1 RETURNING attempts"
        ).fetchone()[0]
        connection.execute("INSERT INTO retry_result VALUES (1) ON CONFLICT DO NOTHING")
    if attempt < 3:
        raise ServiceError(503, "identity_busy", "must not enter native error logs")
    return {"saved": True}


@DBOS.workflow(name="test.retry.workflow")
async def workflow():
    return await persist()


started = asyncio.Event()
cancel_attempts = []


@safe_step(name="test.retry.cancel", idempotent=True, retry_policy=RetryPolicy(), preemptible=True)
async def busy():
    cancel_attempts.append(True)
    started.set()
    raise ServiceError(503, "identity_busy", "busy")


@DBOS.workflow(name="test.retry.cancel_workflow")
async def cancellation():
    return await busy()


async def main():
    with psycopg.connect(sql_url) as connection:
        connection.execute("CREATE TABLE retry_probe(attempts integer)")
        connection.execute("INSERT INTO retry_probe VALUES(0)")
        connection.execute("CREATE TABLE retry_result(id integer PRIMARY KEY)")
    DBOS(
        config={
            "name": "keystone",
            "system_database_url": url,
            "application_version": "retry-test",
            "executor_id": "retry-test",
            "enable_otlp": False,
            "run_admin_server": False,
            "log_level": "CRITICAL",
            "dbos_system_schema": "dbos",
        }
    )
    DBOS.listen_queues([])
    DBOS.launch()
    try:
        identifier = str(uuid4())
        with SetWorkflowID(identifier):
            assert await workflow() == {"saved": True}
        with SetWorkflowID(identifier):
            assert await workflow() == {"saved": True}
        with psycopg.connect(sql_url) as connection:
            assert connection.execute("SELECT attempts FROM retry_probe").fetchone()[0] == 3
            assert connection.execute("SELECT count(*) FROM retry_result").fetchone()[0] == 1
        handle = await DBOS.start_workflow_async(cancellation)
        await asyncio.wait_for(started.wait(), 5)
        await DBOS.cancel_workflow_async(handle.workflow_id)
        try:
            await handle.get_result()
        except (
            dbos_error.DBOSWorkflowCancelledError,
            dbos_error.DBOSAwaitedWorkflowCancelledError,
        ):
            pass
        else:
            raise AssertionError("Cancelled retry workflow succeeded")
        assert len(cancel_attempts) == 1
        print("native-retry-checkpoint-and-cancel-passed", flush=True)
    finally:
        await asyncio.to_thread(DBOS.destroy, workflow_completion_timeout_sec=5)


if __name__ == "__main__":
    run(main())

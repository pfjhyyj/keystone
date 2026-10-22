"""Real persisted schedules, queue isolation and bounded maintenance retries."""

import time
from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, select
from test_files_dictionary import client as client
from test_files_dictionary import upload
from test_flow import configuration as configuration
from test_flow import start, wait_run, worker
from test_identity import query

from keystone.platform.audit.models import AuditLog
from keystone.platform.files.models import File
from keystone.platform.files.tasks import workflows as files_jobs
from keystone.platform.flow.models import FlowRun
from keystone.platform.flow.tasks import workflows as flow_jobs
from keystone.platform.model_access.tasks import workflows as model_jobs

pytestmark = pytest.mark.postgres


def wait_for(client, process, log, condition, timeout=30):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        assert process.poll() is None, log.read_text(encoding="utf8")
        result = condition()
        if result:
            return result
        time.sleep(0.1)
    pytest.fail(log.read_text(encoding="utf8") + "\nMaintenance did not reach expected state")


def schedules(client):
    engine = client.app.state.runtime.flow_engine.client
    return {item["schedule_name"]: item for item in client.portal.call(engine.list_schedules_async)}


def executions(client, name, **filters):
    engine = client.app.state.runtime.flow_engine.client

    async def read():
        return await engine.list_workflows_async(name=name, limit=100, **filters)

    return client.portal.call(read)


def test_schedules_survive_restart_and_flow_toggle_without_duplicate_cleanup(client, tmp_path):
    with worker(client, tmp_path) as (process, log):
        wait_for(
            client,
            process,
            log,
            lambda: client.get("/api/v1/worker/status").json()["status"] == "ready",
        )
        original = schedules(client)
        assert set(original) == {files_jobs.SCHEDULE, flow_jobs.SCHEDULE, model_jobs.SCHEDULE}
        for plan in original.values():
            assert plan["schedule"] == "*/5 * * * * *"
            assert not plan["automatic_backfill"]

    disabled = """
import os
os.environ['KEYSTONE_FLOW_ENABLED'] = 'false'
from keystone.entrypoints.worker import main
main()
"""
    client.app.state.runtime.settings.flow_enabled = False
    with worker(client, tmp_path, code=disabled) as (process, log):
        wait_for(
            client,
            process,
            log,
            # The previous process's last heartbeat is still fresh during restart.
            lambda: schedules(client)[flow_jobs.SCHEDULE]["status"] == "PAUSED",
        )
        paused = schedules(client)
        assert paused[flow_jobs.SCHEDULE]["status"] == "PAUSED"
        assert paused[files_jobs.SCHEDULE]["status"] == "ACTIVE"
        assert {name: plan["schedule_id"] for name, plan in paused.items()} == {
            name: plan["schedule_id"] for name, plan in original.items()
        }
        # Create work after startup, so a real cron tick must discover it.
        identifier = upload(client, key="scheduled-delete").json()["id"]
        assert client.delete(f"/api/v1/files/{identifier}").status_code == 202
        wait_for(
            client,
            process,
            log,
            lambda: (
                query(
                    client,
                    lambda session: session.scalar(
                        select(File.status).where(File.id == UUID(identifier))
                    ),
                )
                == "deleted"
            ),
        )
        assert executions(client, files_jobs.WORKFLOW)

    client.app.state.runtime.settings.flow_enabled = True
    with worker(client, tmp_path) as (process, log):
        wait_for(
            client,
            process,
            log,
            lambda: schedules(client)[flow_jobs.SCHEDULE]["status"] == "ACTIVE",
        )
        resumed = schedules(client)
        assert resumed[flow_jobs.SCHEDULE]["status"] == "ACTIVE"
        assert {name: plan["schedule_id"] for name, plan in resumed.items()} == {
            name: plan["schedule_id"] for name, plan in original.items()
        }
        previous = resumed[flow_jobs.SCHEDULE]["last_fired_at"]
        wait_for(
            client,
            process,
            log,
            lambda: schedules(client)[flow_jobs.SCHEDULE]["last_fired_at"] != previous,
        )
    assert (
        query(client, lambda session: session.scalar(select(func.count()).select_from(FlowRun)))
        == 0
    )
    assert (
        query(
            client,
            lambda session: session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "file.delete.complete", AuditLog.target_id == identifier)
            ),
        )
        == 1
    )


def test_slow_cleanup_is_coalesced_and_does_not_block_flow_coordination(client, tmp_path):
    identifier = start(client, upload(client).json()["id"]).json()["id"]
    code = """
import asyncio
from keystone.platform.files.tasks import workflows as jobs
original = jobs.cleanup
calls = 0
async def slow_once(*args, **kwargs):
    global calls
    calls += 1
    if calls == 1:
        print('slow_cleanup_entered', flush=True)
        await asyncio.sleep(15)
    await original(*args, **kwargs)
jobs.cleanup = slow_once
from keystone.entrypoints.worker import main
main()
"""
    with worker(client, tmp_path, code=code) as (process, log):
        done = wait_run(client, identifier, process, log)
        assert done["status"] == "succeeded", done
        wait_for(
            client,
            process,
            log,
            lambda: len(executions(client, "keystone.files.cleanup_tick", status="SUCCESS")) >= 2,
        )
        files = executions(client, files_jobs.WORKFLOW)
        assert len(files) == 1 and files[0].status == "PENDING"
        assert len(executions(client, flow_jobs.WORKFLOW, status="SUCCESS")) >= 2
        observation = client.get("/api/v1/worker/status").json()
        assert observation["status"] == "unready" and observation["last_heartbeat_at"]
        wait_for(
            client,
            process,
            log,
            lambda: client.get("/api/v1/worker/status").json()["status"] == "ready",
        )


def test_failed_cleanup_retries_on_next_tick_without_stopping_worker_or_exposing_error(
    client, tmp_path
):
    code = """
from keystone.platform.files.tasks import workflows as jobs
original = jobs.cleanup
failed = False
async def fail_once(*args, **kwargs):
    global failed
    if not failed:
        failed = True
        raise RuntimeError('password=maintenance-secret')
    return await original(*args, **kwargs)
jobs.cleanup = fail_once
from keystone.entrypoints.worker import main
main()
"""
    with worker(client, tmp_path, code=code) as (process, log):
        wait_for(
            client,
            process,
            log,
            lambda: (
                executions(client, files_jobs.WORKFLOW, status="ERROR")
                and executions(client, files_jobs.WORKFLOW, status="SUCCESS")
                and client.get("/api/v1/worker/status").json()["status"] == "ready"
            ),
        )
        errors = executions(client, files_jobs.WORKFLOW, status="ERROR")
        assert all(str(row.error) == "file_cleanup_failed" for row in errors)
        engine = client.app.state.runtime.flow_engine.client
        for row in errors:
            steps = client.portal.call(engine.list_workflow_steps_async, row.workflow_id)
            failures = [step["error"] for step in steps if step.get("error")]
            assert failures and all(str(error) == "file_cleanup_failed" for error in failures)
        assert "maintenance-secret" not in log.read_text(encoding="utf8")
        assert "file_cleanup_failed" in log.read_text(encoding="utf8")


def test_interrupted_cleanup_recovers_same_execution_after_worker_restart(client, tmp_path):
    identifier = upload(client, key="interrupted-delete").json()["id"]
    assert client.delete(f"/api/v1/files/{identifier}").status_code == 202
    code = """
import asyncio
from keystone.platform.files.tasks import workflows as jobs
async def interrupted(*args, **kwargs):
    print('cleanup_waiting_for_crash', flush=True)
    await asyncio.Event().wait()
jobs.cleanup = interrupted
from keystone.entrypoints.worker import main
main()
"""
    with worker(client, tmp_path, code=code) as (process, log):
        wait_for(
            client,
            process,
            log,
            lambda: "cleanup_waiting_for_crash" in log.read_text(encoding="utf8"),
        )
        pending = executions(client, files_jobs.WORKFLOW, status="PENDING")
        assert len(pending) == 1
        execution_id = pending[0].workflow_id
        # The context manager kills the process, leaving DBOS recovery state intact.

    with worker(client, tmp_path) as (process, log):
        wait_for(
            client,
            process,
            log,
            lambda: any(
                row.workflow_id == execution_id
                for row in executions(client, files_jobs.WORKFLOW, status="SUCCESS")
            ),
        )
        assert (
            query(
                client,
                lambda session: session.scalar(
                    select(File.status).where(File.id == UUID(identifier))
                ),
            )
            == "deleted"
        )
    assert (
        query(
            client,
            lambda session: session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "file.delete.complete", AuditLog.target_id == identifier)
            ),
        )
        == 1
    )


def test_old_version_maintenance_cannot_hold_new_version_deduplication(client, tmp_path):
    engine = client.app.state.runtime.flow_engine.client

    async def old_work():
        for job in (files_jobs, flow_jobs):
            for suffix in ("", ":previous-release"):
                await engine.enqueue_async(
                    {
                        "workflow_name": job.WORKFLOW,
                        "queue_name": job.QUEUE,
                        "workflow_id": f"old:{job.SCHEDULE}{suffix}",
                        "app_version": "previous-release",
                        "deduplication_id": f"{job.SCHEDULE}{suffix}",
                    }
                )

    client.portal.call(old_work)
    with worker(client, tmp_path) as (process, log):
        wait_for(
            client,
            process,
            log,
            lambda: (
                executions(client, files_jobs.WORKFLOW, status="SUCCESS")
                and executions(client, flow_jobs.WORKFLOW, status="SUCCESS")
                and client.get("/api/v1/worker/status").json()["status"] == "ready"
            ),
        )
        for job in (files_jobs, flow_jobs):
            old = [
                row
                for row in executions(client, job.WORKFLOW)
                if row.app_version == "previous-release"
            ]
            assert len(old) == 2 and all(row.status == "ENQUEUED" for row in old)


def test_cost_scan_recovers_checkpointed_scopes_and_native_child_enqueues(client, tmp_path):
    checkpoint = tmp_path / "interrupted-cost-scan.txt"
    first = f"""
import asyncio
from pathlib import Path
from dbos import DBOS
from keystone.platform.model_access.tasks import workflows as jobs
async def scopes(self): return ['a' * 64, 'b' * 64]
async def reconcile(self, scope): pass
jobs.CostService.scopes, jobs.CostService.reconcile = scopes, reconcile
original = jobs.enqueue_task
async def interrupt_after_enqueue(*args, **kwargs):
    result = await original(*args, **kwargs)
    if args[1]['scope'] == 'a' * 64:
        Path({str(checkpoint)!r}).write_text(DBOS.workflow_id, encoding='utf8')
        print('cost_scan_enqueued_first_scope', flush=True)
        await asyncio.Event().wait()
    return result
jobs.enqueue_task = interrupt_after_enqueue
from keystone.entrypoints.worker import main
main()
"""
    with worker(client, tmp_path, code=first) as (process, log):
        wait_for(
            client,
            process,
            log,
            lambda: "cost_scan_enqueued_first_scope" in log.read_text(encoding="utf8"),
        )
        identifier = checkpoint.read_text(encoding="utf8")
        assert any(
            row.workflow_id == identifier
            for row in executions(
                client,
                model_jobs.WORKFLOW,
                status="PENDING",
            )
        )

    second = f"""
from dbos import DBOS
from keystone.platform.model_access.tasks import workflows as jobs
async def scopes(self):
    assert DBOS.workflow_id != {identifier!r}, 'must replay the saved scope list'
    return []
async def reconcile(self, scope): pass
jobs.CostService.scopes, jobs.CostService.reconcile = scopes, reconcile
from keystone.entrypoints.worker import main
main()
"""
    with worker(client, tmp_path, code=second) as (process, log):
        wait_for(
            client,
            process,
            log,
            lambda: any(
                row.workflow_id == identifier
                for row in executions(
                    client,
                    model_jobs.WORKFLOW,
                    status="SUCCESS",
                )
            ),
        )
        children = [
            row
            for row in executions(client, model_jobs.QUERY_WORKFLOW)
            if row.workflow_id.startswith(f"cost-query:{identifier}:")
        ]
        assert {row.workflow_id for row in children} == {
            f"cost-query:{identifier}:{scope * 64}" for scope in ("a", "b")
        }


def test_independent_task_delivery_reuses_completed_execution_key(client, tmp_path):
    code = """
from dbos import DBOS
from keystone.core.execution.contracts import NoInput, TaskBinding, TaskDefinition
from keystone.core.execution.dispatch import enqueue_task
from keystone.platform.files.tasks.definitions import CLEANUP
import keystone.entrypoints.worker as worker

definition = TaskDefinition('test.independent', 'keystone.test.independent', NoInput, CLEANUP.queue)
@DBOS.workflow(name=definition.entrypoint)
async def independent(payload):
    print('independent_task_executed', flush=True)
binding = TaskBinding(definition, independent)
original = worker.register_maintenance
def register(runtime, health):
    queues, installers = original(runtime, health)
    async def probe():
        for key in ('independent-first', 'independent-first', 'independent-second'):
            ref = await enqueue_task(binding, {}, execution_key=key)
            assert ref.workflow_id == key
            handle = await DBOS.retrieve_workflow_async(ref.workflow_id)
            await handle.get_result()
        print('independent_delivery_checked', flush=True)
    return queues, [*installers, probe]
worker.register_maintenance = register
worker.main()
"""
    with worker(client, tmp_path, code=code) as (process, log):
        wait_for(
            client,
            process,
            log,
            lambda: "independent_delivery_checked" in log.read_text(encoding="utf8"),
        )
        rows = executions(client, "keystone.test.independent")
        assert {row.workflow_id for row in rows} == {"independent-first", "independent-second"}
        assert all(row.status == "SUCCESS" for row in rows)
        assert log.read_text(encoding="utf8").count("independent_task_executed") == 2


def test_flow_scan_attempts_advance_fairly_across_scheduled_passes(client, tmp_path):
    identifier = start(client, upload(client).json()["id"]).json()["id"]

    async def older_runs(session):
        current = await session.get(FlowRun, UUID(identifier))
        for index in range(1, 26):
            session.add(
                FlowRun(
                    id=UUID(int=index),
                    created_at=current.created_at - timedelta(hours=1),
                    definition_key=current.definition_key,
                    definition_snapshot=current.definition_snapshot,
                    execution_version="unavailable",
                    actor_id=current.actor_id,
                    personal_access_token_id=None,
                    request_key=f"older-{index}",
                    request_digest=f"{index:064x}",
                    input=current.input,
                )
            )

    query(client, older_runs)
    with worker(client, tmp_path) as (process, log):
        done = wait_run(client, identifier, process, log)
        assert done["status"] == "succeeded", done
        assert len(executions(client, flow_jobs.WORKFLOW, status="SUCCESS")) >= 3
    assert (
        query(
            client,
            lambda session: session.scalar(
                select(func.count()).select_from(FlowRun).where(FlowRun.status == "pending")
            ),
        )
        == 25
    )

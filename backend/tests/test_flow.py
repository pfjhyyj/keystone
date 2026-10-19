import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from uuid import UUID

import pytest
from dbos import DBOS
from pydantic import SecretStr
from sqlalchemy import select, update
from test_files_dictionary import client as client
from test_files_dictionary import upload
from test_identity import assign, bearer, create_user, login, query, role, token
from test_model_access import channel, entry

from keystone.core.errors import ServiceError
from keystone.core.worker_health import WorkerHealth
from keystone.platform.audit.models import AuditLog
from keystone.platform.audit.services import writer as audit
from keystone.platform.files.models import FileReference
from keystone.platform.flow.engine import Engine
from keystone.platform.flow.models import FlowRun
from keystone.platform.flow.reconcile import reconcile_one
from keystone.platform.model_access.models import ModelInvocation

pytestmark = pytest.mark.postgres


@pytest.fixture(autouse=True)
def configuration(client):
    runtime = client.app.state.runtime
    settings = runtime.settings
    settings.model_mode, settings.flow_enabled = "mock", True
    settings.model_secrets = {k: SecretStr("fake-only") for k in ("inference", "billing")}
    DBOS.migrate(settings.database_url.get_secret_value())
    runtime.flow_engine = Engine(settings)
    entry(client, channel(client))


def start(client, file_id, key="review", **overrides):
    return client.post(
        "/api/v1/flow-runs",
        json={
            "definition_key": "document.review",
            "request_key": key,
            "input": {"source_file_id": file_id, "model_key": "chat"},
            **overrides,
        },
    )


@contextmanager
def worker(client, tmp_path, *, code=None):
    settings = client.app.state.runtime.settings
    env = {
        **os.environ,
        "KEYSTONE_ENV": "test",
        "KEYSTONE_FLOW_ENABLED": "true",
        "KEYSTONE_DATABASE_URL": settings.database_url.get_secret_value(),
        "KEYSTONE_FILE_COORDINATION_ROOT": str(settings.file_coordination_root),
        "KEYSTONE_FILE_STORAGES": json.dumps(
            {
                key: (
                    source.model_dump(mode="json", exclude={"access_key", "secret_key"})
                    | (
                        {
                            "access_key": source.access_key.get_secret_value(),
                            "secret_key": source.secret_key.get_secret_value(),
                        }
                        if source.driver == "s3"
                        else {}
                    )
                )
                for key, source in settings.file_storages.items()
            }
        ),
        "KEYSTONE_FILE_DEFAULT_STORAGE": settings.file_default_storage,
        "KEYSTONE_MODEL_MODE": "mock",
        "KEYSTONE_MODEL_SECRETS": json.dumps(
            {k: v.get_secret_value() for k, v in settings.model_secrets.items()}
        ),
    }
    log = tmp_path / f"worker-{time.time_ns()}.log"
    env["KEYSTONE_WORKER_READY_FILE"] = str(log.with_suffix(".ready"))
    with log.open("w", encoding="utf8") as output:
        process = subprocess.Popen(
            [sys.executable, "-c", code or "from keystone.entrypoints.worker import main; main()"],
            env=env,
            stdout=output,
            stderr=output,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        try:
            yield process, log
        finally:
            if process.poll() is None:
                if sys.platform == "win32":
                    # Windows venv Python can launch a base-interpreter child.
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        check=False,
                        capture_output=True,
                    )
                else:
                    process.terminate()
            process.wait(timeout=20)


def wait_run(client, identifier, process, log, timeout=35):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        row = client.get("/api/v1/flow-runs/" + identifier).json()
        if row["status"] in {"succeeded", "failed", "cancelled"}:
            return row
        assert process.poll() is None, log.read_text(encoding="utf8")
        time.sleep(0.1)
    pytest.fail(log.read_text(encoding="utf8") + "\nRun did not reach terminal state")


def test_real_worker_harness_model_and_report(client, tmp_path):
    file_id = upload(client, b"A small document for review.").json()["id"]
    response = start(client, file_id)
    assert response.status_code == 202, response.text
    row = response.json()
    assert len(row) == 16 and row["status"] == "pending"
    with worker(client, tmp_path) as (process, log):
        done = wait_run(client, row["id"], process, log)
        assert done["status"] == "succeeded", (done, log.read_text(encoding="utf8"))
        assert done["result"]["verdict"] == "reviewed"
        content = client.get(f"/api/v1/files/{done['result']['report_file_id']}/content")
        assert content.status_code == 200 and "模拟" in content.text
        history = client.get(f"/api/v1/flow-runs/{row['id']}/steps").json()
        assert history["available"] and all(
            set(s)
            <= {"workflow_id", "step_id", "name", "started_at_epoch_ms", "completed_at_epoch_ms"}
            for s in history["items"]
        )
        calls = client.get(f"/api/v1/model-invocations?flow_run_id={row['id']}").json()
        assert len(calls) == 2 and {c["status"] for c in calls} == {"succeeded"}
        assert len({c["flow_context"]["agent_call_id"] for c in calls}) == 1
        assert (
            client.post(f"/api/v1/flow-runs/{row['id']}/cancel").json()["cancel_requested_at"]
            is None
        )
    refs = query(
        client,
        lambda s: s.scalars(
            select(FileReference).where(FileReference.owner == f"flow:{row['id']}")
        ),
    ).all()
    assert {r.slot for r in refs} == {"input", "artifact.report", "output"}
    assert client.delete(f"/api/v1/files/{file_id}").status_code == 409


@pytest.mark.parametrize("credential_kind", ["session", "pat"])
def test_start_and_input_reference_audits_share_request_id(client, credential_kind):
    file_id = upload(client).json()["id"]
    pat = None
    if credential_kind == "pat":
        pat = token(
            client,
            [
                "flow:start",
                "flow:read",
                "file:create",
                "file:inspect",
                "file:content_read",
                "file:manage",
                "model:invoke",
            ],
        )
        client.headers.update(bearer(pat["token"]))
    response = start(client, file_id)
    assert response.status_code == 202, response.text
    run = response.json()

    def logs():
        return query(
            client,
            lambda session: session.scalars(
                select(AuditLog).where(
                    ((AuditLog.action == "flow.start") & (AuditLog.target_id == run["id"]))
                    | ((AuditLog.action == "file.reference") & (AuditLog.target_id == file_id))
                )
            ),
        ).all()

    rows = logs()
    assert {row.action for row in rows} == {"flow.start", "file.reference"}
    assert len(rows) == 2
    for row in rows:
        assert row.correlation_id == response.headers["X-Request-ID"]
        assert row.actor_type == "user" and str(row.actor_id) == run["actor_id"]
        assert row.outcome == "success"
        if pat:
            assert row.details["credential_id"] == pat["id"]
    reference = next(row for row in rows if row.action == "file.reference")
    assert reference.details["owner"] == f"flow:{run['id']}"
    assert reference.details["slot"] == "input" and reference.details["added"] is True

    repeated = start(client, file_id)
    assert repeated.status_code == 202 and repeated.json()["id"] == run["id"]
    assert repeated.headers["X-Request-ID"] != response.headers["X-Request-ID"]
    after = logs()
    assert {row.id for row in after} == {row.id for row in rows}
    assert {row.correlation_id for row in after} == {response.headers["X-Request-ID"]}


def test_start_idempotency_cancel_before_delivery(client, tmp_path):
    file_id = upload(client).json()["id"]
    first = start(client, file_id).json()
    assert start(client, file_id).json()["id"] == first["id"]
    assert (
        start(
            client, file_id, input={"source_file_id": file_id, "model_key": "changed"}
        ).status_code
        == 409
    )
    cancel = client.post(f"/api/v1/flow-runs/{first['id']}/cancel").json()
    assert (
        client.post(f"/api/v1/flow-runs/{first['id']}/cancel").json()["cancel_requested_at"]
        == cancel["cancel_requested_at"]
    )
    with worker(client, tmp_path) as (process, log):
        assert wait_run(client, first["id"], process, log)["status"] == "cancelled"
    assert client.get("/api/v1/model-invocations").json() == []


@pytest.mark.parametrize("point", ["registered", "response", "artifact"])
def test_worker_crash_recovery_never_repeats_paid_request(client, tmp_path, point):
    file_id = upload(client).json()["id"]
    if point == "artifact":
        # The declared permissions must cover recovery as well as first execution.
        body = {
            "definition_key": "document.review",
            "request_key": "review",
            "input": {"source_file_id": file_id, "model_key": "chat"},
        }
        permissions = [
            "flow:start",
            "flow:read",
            "file:content_read",
            "file:create",
            "model:invoke",
        ]
        incomplete = token(client, permissions)
        assert (
            client.post(
                "/api/v1/flow-runs", json=body, headers=bearer(incomplete["token"])
            ).status_code
            == 403
        )
        allowed = token(client, permissions + ["file:inspect", "file:manage"])
        response = client.post("/api/v1/flow-runs", json=body, headers=bearer(allowed["token"]))
        assert response.status_code == 202, response.text
        run = response.json()
    else:
        run = start(client, file_id).json()
    if point == "registered":
        injection = """
import httpx2
from keystone.platform.model_access.gateway import Gateway
def transport(self, provider):
    async def crash(request):
        os._exit(71)
    return httpx2.MockTransport(crash)
Gateway.transport = transport
"""
    elif point == "response":
        injection = """
from keystone.platform.flow.agent_model import FlowModel
original = FlowModel.request
async def crash(self, *args, **kwargs):
    await original(self, *args, **kwargs)
    os._exit(71)
FlowModel.request = crash
"""
    else:
        injection = """
from keystone.platform.flow import host
original = host.upload
async def crash(*args, **kwargs):
    await original(*args, **kwargs)
    os._exit(71)
host.upload = crash
"""
    code = "import os\n" + injection + "\nfrom keystone.entrypoints.worker import main; main()"
    with worker(client, tmp_path, code=code) as (process, log):
        assert process.wait(timeout=30) == 71, log.read_text(encoding="utf8")
    before = client.get(f"/api/v1/model-invocations?flow_run_id={run['id']}").json()
    assert len(before) == (2 if point == "artifact" else 1)
    with worker(client, tmp_path) as (process, log):
        done = wait_run(client, run["id"], process, log)
        assert done["status"] == ("succeeded" if point == "artifact" else "failed"), done
        if point != "artifact":
            assert done["error_code"] == "model_result_unavailable", done
    after = client.get(f"/api/v1/model-invocations?flow_run_id={run['id']}").json()
    assert {c["id"] for c in after} == {c["id"] for c in before}
    assert after[0]["status"] == ("unknown" if point == "registered" else "succeeded")
    if point == "artifact":
        assert len(client.get("/api/v1/files").json()) == 2


def test_pat_revocation_blocks_execution_but_new_session_can_cancel(client, tmp_path):
    file_id = upload(client).json()["id"]
    p = token(client, list(client.get("/api/v1/auth/me").json()["permissions"]))
    body = {
        "definition_key": "document.review",
        "request_key": "pat-review",
        "input": {"source_file_id": file_id, "model_key": "chat"},
    }
    row = client.post("/api/v1/flow-runs", json=body, headers=bearer(p["token"])).json()
    assert row["personal_access_token_id"] == p["id"]
    assert client.post("/api/v1/flow-runs", json=body).status_code == 409
    client.delete("/api/v1/auth/tokens/" + p["id"])
    with worker(client, tmp_path) as (process, log):
        done = wait_run(client, row["id"], process, log)
        assert done["status"] == "failed" and done["error_code"] == "execution_not_authorized"
    assert client.get("/api/v1/model-invocations").json() == []
    assert client.post(f"/api/v1/flow-runs/{row['id']}/cancel").status_code == 200


def test_cross_user_scope_and_independent_cancel_and_cost_permissions(client):
    file_id = upload(client).json()["id"]
    row = start(client, file_id).json()
    other = create_user(client, "viewer")
    reader = role(client, "flow-reader", ["flow:read", "flow:cancel"])
    assign(client, other["id"], reader["id"])
    # Test fixture enables the account after verifying the real password-change flow elsewhere.
    from keystone.platform.identity.models import User

    query(
        client,
        lambda s: s.execute(
            update(User).where(User.id == UUID(other["id"])).values(password_change_required=False)
        ),
    )
    login(client, "viewer")
    assert client.get("/api/v1/flow-runs").json() == []
    assert client.post(f"/api/v1/flow-runs/{row['id']}/cancel").status_code == 404
    login(client)
    manager = role(client, "flow-manager", ["flow:read", "flow:manage", "flow:cancel"])
    assign(client, other["id"], manager["id"])
    login(client, "viewer")
    assert client.get(f"/api/v1/flow-runs/{row['id']}").status_code == 200
    assert client.get(f"/api/v1/flow-runs/{row['id']}/costs").status_code == 403
    assert client.get(f"/api/v1/files/{file_id}/content").status_code == 403
    assert client.post(f"/api/v1/flow-runs/{row['id']}/cancel").status_code == 200
    assert "flow:start" not in client.get("/api/v1/auth/me").json()["permissions"]


def test_concurrent_start_and_snapshot_not_re_resolved(client):
    file_id = upload(client).json()["id"]
    with ThreadPoolExecutor() as pool:
        results = list(pool.map(lambda _: start(client, file_id), range(4)))
    assert {r.status_code for r in results} == {202}
    identifiers = {r.json()["id"] for r in results}
    assert len(identifiers) == 1
    original = results[0].json()
    entries = client.get("/api/v1/model-entries").json()
    item = entries[0]
    client.patch(
        "/api/v1/model-entries/" + item["id"],
        json={"channel_id": item["channel_id"], "provider_model": "changed", "is_active": False},
    )
    assert start(client, file_id).json()["input"] == original["input"]
    assert len(client.get("/api/v1/audit-logs?action=flow.start").json()) == 1


def test_original_output_contract_and_retention_after_revocation(client):
    file_id = upload(client).json()["id"]
    pat = token(client, list(client.get("/api/v1/auth/me").json()["permissions"]))
    response = client.post(
        "/api/v1/flow-runs",
        headers=bearer(pat["token"]),
        json={
            "definition_key": "document.review",
            "request_key": "receipt",
            "input": {"source_file_id": file_id, "model_key": "chat"},
        },
    )
    row = response.json()
    client.delete("/api/v1/auth/tokens/" + pat["id"])
    client.patch("/api/v1/files/" + file_id, json={"name": "disabled.txt", "status": "disabled"})

    class Finished:
        async def state(self, identifier):
            return {
                "status": "SUCCESS",
                "version": row["execution_version"],
                "name": "keystone.document.review",
                "error_code": None,
                "result": {"verdict": "reviewed", "summary": "ok", "report_file_id": file_id},
            }

    client.portal.call(reconcile_one, client.app.state.runtime, Finished(), UUID(row["id"]))
    done = client.get("/api/v1/flow-runs/" + row["id"]).json()
    assert done["status"] == "succeeded" and done["personal_access_token_id"] == pat["id"]
    refs = query(
        client,
        lambda s: s.scalars(
            select(FileReference).where(FileReference.owner == f"flow:{row['id']}")
        ),
    ).all()
    assert {r.slot for r in refs} == {"input", "output"}


def test_unregistered_output_file_rejected_and_old_version_not_dispatched(client):
    file_id = upload(client).json()["id"]
    unregistered = upload(client, b"other", key="other").json()["id"]
    row = start(client, file_id).json()

    class Finished:
        async def state(self, identifier):
            return {
                "status": "SUCCESS",
                "version": row["execution_version"],
                "name": "keystone.document.review",
                "error_code": None,
                "result": {"verdict": "reviewed", "summary": "ok", "report_file_id": unregistered},
            }

    client.portal.call(reconcile_one, client.app.state.runtime, Finished(), UUID(row["id"]))
    assert (
        client.get("/api/v1/flow-runs/" + row["id"]).json()["error_code"] == "flow_invalid_output"
    )
    pending = start(client, file_id, "old").json()
    query(
        client,
        lambda s: s.execute(
            update(FlowRun)
            .where(FlowRun.id == UUID(pending["id"]))
            .values(execution_version="old-version")
        ),
    )
    client.portal.call(
        reconcile_one,
        client.app.state.runtime,
        client.app.state.runtime.flow_engine,
        UUID(pending["id"]),
    )
    unavailable = client.get("/api/v1/flow-runs/" + pending["id"]).json()
    assert unavailable["status"] == "pending" and unavailable["observed_at"] is not None


def test_start_audit_failure_rolls_back_run_and_references(client, monkeypatch):
    file_id = upload(client).json()["id"]
    original = audit.record

    async def fail(session, action, *args, **kwargs):
        if action == "flow.start":
            raise RuntimeError("audit unavailable")
        return await original(session, action, *args, **kwargs)

    monkeypatch.setattr(audit, "record", fail)
    client.raise_server_exceptions = False
    assert start(client, file_id).status_code == 500
    assert client.get("/api/v1/flow-runs").json() == []
    assert query(client, lambda s: s.scalars(select(FileReference))).all() == []


def test_cancel_during_model_send_preserves_inflight_receipt(client, tmp_path):
    file_id = upload(client).json()["id"]
    row = start(client, file_id).json()
    code = """
import asyncio
import httpx2
from keystone.platform.model_access.gateway import Gateway
from keystone.platform.model_access import mock
def transport(self, provider):
    async def respond(request):
        await asyncio.sleep(4)
        return await mock.transport(provider).handle_async_request(request)
    return httpx2.MockTransport(respond)
Gateway.transport = transport
from keystone.entrypoints.worker import main
main()
"""
    with worker(client, tmp_path, code=code) as (process, log):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            calls = client.get(f"/api/v1/model-invocations?flow_run_id={row['id']}").json()
            if calls:
                break
            time.sleep(0.1)
        assert calls and calls[0]["status"] == "running"
        assert client.post(f"/api/v1/flow-runs/{row['id']}/cancel").status_code == 200
        assert wait_run(client, row["id"], process, log)["status"] == "cancelled"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            calls = client.get(f"/api/v1/model-invocations?flow_run_id={row['id']}").json()
            if calls[0]["status"] != "running":
                break
            time.sleep(0.1)
        assert len(calls) == 1 and calls[0]["status"] == "succeeded"
        assert calls[0]["cost_amount"] is not None


def test_recovery_only_closes_flow_calls_not_live_api_calls(client):
    from test_model_access import invoke

    from keystone.platform.model_access.services.recovery import recover_interrupted_calls

    first = invoke(client).json()["invocation_id"]
    second = invoke(client).json()["invocation_id"]

    async def prepare(session):
        await session.execute(update(ModelInvocation).values(status="running", finished_at=None))
        await session.execute(
            update(ModelInvocation)
            .where(ModelInvocation.id == UUID(second))
            .values(flow_context={"flow_run_id": str(UUID(int=1))})
        )

    query(client, prepare)
    client.portal.call(recover_interrupted_calls, client.app.state.runtime.database)
    assert client.get("/api/v1/model-invocations/" + first).json()["status"] == "running"
    assert client.get("/api/v1/model-invocations/" + second).json()["status"] == "unknown"
    assert client.portal.call(recover_interrupted_calls, client.app.state.runtime.database) == 0
    logs = client.get("/api/v1/audit-logs?action=model.invocation.recover").json()
    assert len(logs) == 1 and logs[0]["target_id"] == second
    assert logs[0]["actor_type"] == "system"
    assert logs[0]["details"] == {"source": "flow_worker_recovery"}


def test_execution_context_preserves_transaction_errors_and_audit(client):
    from keystone.platform.flow.identity import ExecutionIdentity

    file_id = upload(client).json()["id"]
    row = start(client, file_id).json()
    context = ExecutionIdentity(client.app.state.runtime, row["id"])

    async def conflict():
        async with context.access(
            "file:create", write=True, action="file.reference", target="file", target_id=file_id
        ) as (session, _):
            session.add(
                FileReference(owner=f"flow:{row['id']}", slot="input", file_id=UUID(file_id))
            )
            await session.flush()

    async def session_only():
        async with context.access(session_only=True, action="file.reference"):
            pytest.fail("A trusted execution must not become a login session")

    for operation, code in ((conflict, "conflict"), (session_only, "session_required")):
        with pytest.raises(ServiceError) as error:
            client.portal.call(operation)
        assert error.value.code == code
    logs = client.get("/api/v1/audit-logs?action=file.reference").json()
    failures = {r["details"]["reason"]: r for r in logs if r["outcome"] != "success"}
    assert failures["conflict"]["outcome"] == "failure"
    assert failures["conflict"]["target_id"] == file_id
    assert failures["session_required"]["outcome"] == "denied"
    assert all(r["actor_id"] == row["actor_id"] for r in failures.values())


def test_flow_binary_files_limits_and_idempotent_retention(client, monkeypatch):
    from keystone.platform.flow import host

    client.app.state.runtime.settings.file_max_bytes = 32768
    data = b"\xff\x00\x81" * 6000
    source = upload(client, data).json()["id"]
    run_id = start(client, source).json()["id"]
    monkeypatch.setattr(host, "_runtime", client.app.state.runtime)

    async def exercise():
        assert await host.read_file(run_id, source, max_bytes=len(data)) == data
        original = client.app.state.runtime.storage.get().read

        def unexpected_read(*args):
            pytest.fail("Reject the oversized input before reading storage")

        monkeypatch.setattr(client.app.state.runtime.storage.get(), "read", unexpected_read)
        with pytest.raises(ServiceError) as error:
            await host.read_file(run_id, source, max_bytes=len(data) - 1)
        assert error.value.code == "file_too_large"
        monkeypatch.setattr(client.app.state.runtime.storage.get(), "read", original)
        identifier = await host.upload(
            run_id, "binary", "data.bin", data, content_type="application/octet-stream"
        )
        assert (
            await host.upload(
                run_id, "binary", "data.bin", data, content_type="application/octet-stream"
            )
            == identifier
        )
        assert await host.read_file(run_id, identifier, max_bytes=len(data)) == data
        return identifier

    identifier = client.portal.call(exercise)
    assert (
        client.get(f"/api/v1/files/{identifier}").json()["content_type"]
        == "application/octet-stream"
    )
    refs = client.get(f"/api/v1/files/{identifier}/references").json()
    assert refs == [{"owner": f"flow:{run_id}", "slot": "artifact.binary"}]


def test_upload_completion_retains_file_after_flow_cancel(client, monkeypatch):
    from keystone.core.time import utcnow
    from keystone.platform.files.services import files
    from keystone.platform.flow import host

    runtime = client.app.state.runtime
    run_id = start(client, upload(client).json()["id"]).json()["id"]
    monkeypatch.setattr(host, "_runtime", runtime)
    original = files.blocking

    async def cancel_after_write(function, *args):
        result = await original(function, *args)
        if function.__name__ == "write":
            async with runtime.database.transaction() as session:
                await session.execute(
                    update(FlowRun)
                    .where(FlowRun.id == UUID(run_id))
                    .values(cancel_requested_at=utcnow())
                )
        return result

    monkeypatch.setattr(files, "blocking", cancel_after_write)

    async def upload_report():
        return await host.upload(
            run_id, "report", "cancelled.txt", b"ok", content_type="text/plain"
        )

    with pytest.raises(ServiceError) as error:
        client.portal.call(upload_report)
    assert error.value.code == "flow_cancelled"
    file = next(r for r in client.get("/api/v1/files").json() if r["name"] == "cancelled.txt")
    assert file["status"] == "active"
    assert client.get(f"/api/v1/files/{file['id']}/references").json() == [
        {"owner": f"flow:{run_id}", "slot": "artifact.report"}
    ]
    assert client.delete(f"/api/v1/files/{file['id']}").status_code == 409


def test_worker_single_instance_and_orderly_stop(client, tmp_path):
    stop_file = tmp_path / "stop-worker"
    code = f"""
import asyncio
from pathlib import Path
from keystone.core.asyncio import run
from keystone.core.config import Settings
from keystone.core.logging import configure_logging
from keystone.entrypoints.worker import run as worker_run
async def main():
    stop = asyncio.Event()
    task = asyncio.create_task(worker_run(Settings(), stop))
    while not Path({str(stop_file)!r}).exists():
        if task.done():
            await task
        await asyncio.sleep(0.1)
    stop.set()
    await task
configure_logging('INFO')
run(main())
"""
    with worker(client, tmp_path, code=code) as (process, log):
        deadline = time.monotonic() + 20
        health = WorkerHealth(log.with_suffix(".ready"))
        while not health.healthy():
            assert process.poll() is None and time.monotonic() < deadline
            time.sleep(0.1)
        with worker(client, tmp_path) as (other, _):
            assert other.wait(timeout=15) != 0
        assert health.healthy()
        stop_file.touch()
        assert process.wait(timeout=20) == 0, log.read_text(encoding="utf8")
        assert "worker_stopped" in log.read_text(encoding="utf8")
        assert not health.path.exists()

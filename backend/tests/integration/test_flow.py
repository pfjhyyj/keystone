import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

import pytest
from dbos import DBOS
from sqlalchemy import select, text, update
from test_files_dictionary import client as client
from test_files_dictionary import upload
from test_identity import assign, bearer, create_user, login, query, role, token
from test_model_access import channel, entry

from keystone.composition import execution_version
from keystone.core.errors import ServiceError
from keystone.platform.audit.models import AuditLog
from keystone.platform.audit.services import writer as audit
from keystone.platform.files.models import FileReference
from keystone.platform.flow.engine import Engine
from keystone.platform.flow.models import FlowRun
from keystone.platform.flow.tasks.workflows import reconcile_one
from keystone.platform.model_access.models import ModelInvocation

pytestmark = pytest.mark.postgres


@pytest.fixture(autouse=True)
def configuration(client, monkeypatch):
    from support.model_stub import install

    install(monkeypatch)
    runtime = client.app.state.runtime
    from support.flow_fixture.contracts import definition

    runtime.flow_definitions = {definition.key: definition}
    settings = runtime.settings
    settings.flow_enabled = True
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
def worker(client, tmp_path, *, code=None, stub=True, worker_id=None):
    settings = client.app.state.runtime.settings
    env = {
        **{
            key: value
            for key, value in os.environ.items()
            if not key.startswith("KEYSTONE_FILE_STORAGES")
        },
        "PYTHONPATH": os.pathsep.join(
            filter(
                None,
                [
                    str(Path(__file__).parents[1]),
                    str(Path(__file__).parent),
                    os.getenv("PYTHONPATH"),
                ],
            )
        ),
        "KEYSTONE_ENV": "test",
        "KEYSTONE_FLOW_ENABLED": str(settings.flow_enabled).lower(),
        "KEYSTONE_WORKER_ID": worker_id or settings.worker_id,
        "KEYSTONE_WORKER_IDS": __import__("json").dumps(settings.worker_ids),
        "KEYSTONE_DATABASE_URL": settings.database_url.get_secret_value(),
        "KEYSTONE_FILE_COORDINATION_ROOT": str(settings.file_coordination_root),
        "KEYSTONE_FILE_DEFAULT_STORAGE": settings.file_default_storage,
    }
    for name in (
        "flow_worker_concurrency",
        "flow_global_concurrency",
        "model_cost_concurrency",
        "model_inference_concurrency",
        "model_scope_concurrency",
        "worker_shutdown_seconds",
        "file_max_bytes",
        "file_transfer_slots",
        "file_temp_bytes",
        "file_min_free_bytes",
        "file_upload_timeout_seconds",
    ):
        env["KEYSTONE_" + name.upper()] = str(getattr(settings, name))
    for key, source in settings.file_storages.items():
        for field, value in source.model_dump(mode="json", exclude_none=True).items():
            if field in {"access_key", "secret_key"}:
                value = getattr(source, field).get_secret_value()
            env[f"KEYSTONE_FILE_STORAGES__{key.upper()}__{field.upper()}"] = str(value)
    log = tmp_path / f"worker-{time.time_ns()}.log"
    with log.open("w", encoding="utf8") as output:
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "from support.model_stub import install; install(); "
                    "from support.flow_fixture import install; install(); "
                    if stub
                    else ""
                )
                + (code or "from keystone.entrypoints.worker import main; main()"),
            ],
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


@pytest.mark.parametrize(
    "scenario,status,error,count",
    [
        ("repair", "succeeded", None, 3),
        ("invalid", "failed", "flow_invalid_output", 3),
        ("budget", "failed", "flow_budget_exceeded", 4),
    ],
)
def test_example_acceptance_and_budget_in_real_worker(
    client, tmp_path, scenario, status, error, count
):
    file_id = upload(client, b"Review this source.").json()["id"]
    run = start(client, file_id).json()
    # Only model response contents change. SDK, DBOS and accounting stay real.
    code = (
        f"SCENARIO = {scenario!r}\n"
        + """
from pydantic_ai.messages import ToolCallPart
from keystone.platform.flow.agent_model import FlowModel
submissions = 0
original_request = FlowModel.request

async def edited_request(self, *args, **kwargs):
    global submissions
    response = await original_request(self, *args, **kwargs)
    function = next(part for part in response.parts if isinstance(part, ToolCallPart))
    if function.tool_name == 'submit_review':
        submissions += 1
        if SCENARIO == 'budget':
            function.tool_name, function.args = 'inspect_document', {}
        elif SCENARIO == 'invalid' or submissions == 1:
            values = function.args_as_dict()
            function.args = {**values, 'characters': values['characters'] + 1}
    return response

FlowModel.request = edited_request
from keystone.entrypoints.worker import main
main()
"""
    )
    with worker(client, tmp_path, code=code) as (process, log):
        done = wait_run(client, run["id"], process, log)
    assert (done["status"], done["error_code"]) == (status, error), done
    calls = client.get(f"/api/v1/model-invocations?flow_run_id={run['id']}").json()
    assert len(calls) == count and all(call["status"] == "succeeded" for call in calls)
    assert len({call["id"] for call in calls}) == count
    assert len({call["flow_context"]["agent_call_id"] for call in calls}) == 1
    assert len(client.get("/api/v1/files").json()) == (2 if status == "succeeded" else 1)
    if status == "succeeded":
        assert done["result"]["source_file_id"] == file_id
        assert done["result"]["characters"] == len("Review this source.")
    else:
        assert done["result"] is None


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
from keystone.platform.model_access.http import HttpClient
def transport(self, provider):
    async def crash(request):
        os._exit(71)
    return httpx2.MockTransport(crash)
HttpClient.transport = transport
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
                "result": {
                    "source_file_id": file_id,
                    "characters": 5,
                    "verdict": "reviewed",
                    "summary": "ok",
                    "report_file_id": file_id,
                },
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


@pytest.mark.parametrize("change", ["revoked", "cancelled"])
def test_coordinator_replay_rechecks_identity_and_cancel_intent(client, change):
    file_id = upload(client).json()["id"]
    pat = token(client, list(client.get("/api/v1/auth/me").json()["permissions"]))
    response = client.post(
        "/api/v1/flow-runs",
        headers=bearer(pat["token"]),
        json={
            "definition_key": "document.review",
            "request_key": "replayed-plan",
            "input": {"source_file_id": file_id, "model_key": "chat"},
        },
    )
    assert response.status_code == 202, response.text
    identifier = response.json()["id"]
    if change == "revoked":
        client.delete("/api/v1/auth/tokens/" + pat["id"])
    else:
        assert client.post(f"/api/v1/flow-runs/{identifier}/cancel").status_code == 200

    class NotSubmitted:
        async def state(self, identifier):
            return None

        async def enqueue(self, *args):
            pytest.fail("A replayed plan must not bypass current authorization/cancellation")

    async def replayed_plan(identifier):
        return "submit"  # DBOS has already checkpointed this before the change.

    async def synchronize():
        await reconcile_one(
            client.app.state.runtime,
            NotSubmitted(),
            UUID(identifier),
            prepare=replayed_plan,
        )

    client.portal.call(synchronize)
    done = client.get(f"/api/v1/flow-runs/{identifier}").json()
    assert done["status"] == ("failed" if change == "revoked" else "cancelled")
    if change == "revoked":
        assert done["error_code"] == "execution_not_authorized"


def test_replayed_submission_denial_preserves_concurrent_engine_completion(client):
    file_id = upload(client).json()["id"]
    pat = token(client, list(client.get("/api/v1/auth/me").json()["permissions"]))
    row = client.post(
        "/api/v1/flow-runs",
        headers=bearer(pat["token"]),
        json={
            "definition_key": "document.review",
            "request_key": "completion-race",
            "input": {"source_file_id": file_id, "model_key": "chat"},
        },
    ).json()
    client.delete("/api/v1/auth/tokens/" + pat["id"])

    class CompletedDuringAuthorization:
        observed = False

        async def state(self, identifier):
            if not self.observed:
                self.observed = True
                return None
            return {
                "status": "SUCCESS",
                "version": row["execution_version"],
                "name": "keystone.document.review",
                "error_code": None,
                "result": {
                    "source_file_id": file_id,
                    "characters": 5,
                    "verdict": "reviewed",
                    "summary": "ok",
                    "report_file_id": file_id,
                },
            }

        async def enqueue(self, *args):
            pytest.fail("Revoked identity must not submit another execution")

    async def cached_plan(identifier):
        return "submit"

    async def synchronize():
        await reconcile_one(
            client.app.state.runtime,
            CompletedDuringAuthorization(),
            UUID(row["id"]),
            prepare=cached_plan,
        )

    client.portal.call(synchronize)
    done = client.get(f"/api/v1/flow-runs/{row['id']}").json()
    assert done["status"] == "succeeded" and done["error_code"] is None
    assert done["result"]["report_file_id"] == file_id
    assert len(client.get("/api/v1/audit-logs?action=flow.complete").json()) == 1


@pytest.mark.parametrize("mismatch", ["version", "name"])
def test_replayed_cancel_does_not_cancel_a_mismatched_engine_execution(client, mismatch):
    row = start(client, upload(client).json()["id"]).json()
    client.post(f"/api/v1/flow-runs/{row['id']}/cancel")

    class DifferentExecution:
        async def state(self, identifier):
            return {
                "status": "PENDING",
                "version": row["execution_version"],
                "name": "keystone.document.review",
                mismatch: "different-execution",
            }

        async def cancel(self, identifier):
            pytest.fail("A stale cancel plan must not cancel a different execution")

    async def cached_plan(identifier):
        return "cancel"

    async def synchronize():
        await reconcile_one(
            client.app.state.runtime,
            DifferentExecution(),
            UUID(row["id"]),
            prepare=cached_plan,
        )

    client.portal.call(synchronize)
    done = client.get(f"/api/v1/flow-runs/{row['id']}").json()
    assert done["status"] == "pending" and done["cancel_requested_at"] is not None


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
                "result": {
                    "source_file_id": file_id,
                    "characters": 5,
                    "verdict": "reviewed",
                    "summary": "ok",
                    "report_file_id": unregistered,
                },
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


def test_external_package_change_blocks_execution_and_delivery(client, tmp_path, monkeypatch):
    from keystone import composition
    from keystone.composition import execution_version
    from keystone.platform.flow.identity import ExecutionIdentity

    package = tmp_path / "execution_review_package"
    package.mkdir()
    code = package / "__init__.py"
    code.write_text("# original executor\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(
        composition,
        "EXECUTION_PACKAGES",
        {**composition.EXECUTION_PACKAGES, "execution_review_package": ()},
    )
    execution_version.cache_clear()
    try:
        row = start(client, upload(client).json()["id"]).json()
        assert row["execution_version"] == execution_version()
        code.write_text("# incompatible executor\n")
        execution_version.cache_clear()
        assert execution_version() != row["execution_version"]

        async def access():
            async with ExecutionIdentity(client.app.state.runtime, row["id"]).access():
                return True

        with pytest.raises(ServiceError) as error:
            client.portal.call(access)
        assert error.value.code == "execution_version_unavailable"

        class Pending:
            async def state(self, identifier):
                return None

            async def enqueue(self, *args):
                raise AssertionError("Must not dispatch an incompatible execution version")

        client.portal.call(reconcile_one, client.app.state.runtime, Pending(), UUID(row["id"]))
        assert client.get(f"/api/v1/flow-runs/{row['id']}").json()["status"] == "pending"
        code.write_text("# original executor\n")
        execution_version.cache_clear()
        assert client.portal.call(access)
    finally:
        execution_version.cache_clear()


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
from keystone.platform.model_access.http import HttpClient
from support import model_stub as mock
def transport(self, provider):
    async def respond(request):
        await asyncio.sleep(4)
        return await mock.transport(provider).handle_async_request(request)
    return httpx2.MockTransport(respond)
HttpClient.transport = transport
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
    from uuid import uuid4

    from test_model_access import invoke

    from keystone.core.worker_identity import WorkerIdentity
    from keystone.core.worker_registration import identity_lease
    from keystone.platform.model_access.services.recovery import recover_interrupted_calls

    owner = WorkerIdentity("keystone-worker", uuid4(), execution_version())

    first = invoke(client).json()["invocation_id"]
    second = invoke(client).json()["invocation_id"]

    async def prepare(session):
        await session.execute(update(ModelInvocation).values(status="running", finished_at=None))
        await session.execute(
            update(ModelInvocation)
            .where(ModelInvocation.id == UUID(second))
            .values(
                flow_context={"flow_run_id": str(UUID(int=1))},
                worker_id=owner.worker_id,
                worker_instance_id=uuid4(),
                execution_version=owner.execution_version,
            )
        )

    query(client, prepare)

    async def recover():
        runtime = client.app.state.runtime
        async with identity_lease(runtime.database, runtime.storage, owner.worker_id) as lease:
            return await recover_interrupted_calls(runtime.database, owner, lease=lease)

    assert client.portal.call(recover) == 1
    assert client.get("/api/v1/model-invocations/" + first).json()["status"] == "running"
    assert client.get("/api/v1/model-invocations/" + second).json()["status"] == "unknown"
    assert client.portal.call(recover) == 0
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
        while client.get("/api/v1/worker/status").json()["status"] != "ready":
            assert process.poll() is None and time.monotonic() < deadline
            time.sleep(0.1)
        with worker(client, tmp_path) as (other, _):
            assert other.wait(timeout=15) != 0
        assert client.get("/api/v1/worker/status").json()["status"] == "ready"
        stop_file.touch()
        assert process.wait(timeout=20) == 0, log.read_text(encoding="utf8")
        assert "worker_stopped" in log.read_text(encoding="utf8")
        assert client.get("/api/v1/worker/status").json()["status"] == "offline"


def test_common_dbos_runtime_without_flow_keeps_other_tasks_and_recovery(client, tmp_path):
    # A persisted Flow queue must not be consumed just because DBOS is now always on.
    runtime = client.app.state.runtime
    file_id = upload(client).json()["id"]
    identifier = start(client, file_id).json()["id"]
    definition = runtime.flow_definitions["document.review"]

    async def enqueue(session):
        return await session.get(FlowRun, UUID(identifier))

    row = query(client, enqueue)
    client.portal.call(runtime.flow_engine.enqueue, row, definition)
    code = """
import asyncio
from dbos import DBOS, SetWorkflowID
from keystone.bootstrap import build_runtime
from keystone.core.asyncio import run
from keystone.core.config import Settings
from keystone.core.worker_health import worker_status
from keystone.entrypoints import worker

entered = asyncio.Event()
cancelled = asyncio.Event()

@DBOS.workflow(name='test.common_runtime.blocked')
async def blocked():
    entered.set()
    try:
        await asyncio.Event().wait()
    finally:
        cancelled.set()

async def main():
    settings = Settings()
    settings.flow_enabled = False
    def unexpected_registration():
        raise AssertionError('Disabled Flow registered business execution code')
    worker.register_workflows = unexpected_registration
    runtime = build_runtime(settings)
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(settings, stop))
    other = None
    try:
        async with asyncio.timeout(25):
            while (await worker_status(runtime.database))['status'] != 'ready':
                if task.done():
                    await task
                await asyncio.sleep(.1)
        assert DBOS.executor_id == 'keystone-worker'
        # This sibling is created after DBOS launch; it is not owned by the runtime.
        other = asyncio.create_task(asyncio.Event().wait())
        with SetWorkflowID('test:common-runtime:pending'):
            await DBOS.start_workflow_async(blocked)
        await asyncio.wait_for(entered.wait(), 5)
        stop.set()
        await asyncio.wait_for(task, 20)
        assert cancelled.is_set(), 'Runtime left its in-flight workflow alive'
        assert not other.done(), 'Runtime cancelled a task owned by another component'
        assert await asyncio.to_thread(lambda: 'usable') == 'usable'
        print('common_runtime_verified', flush=True)
    finally:
        stop.set()
        if other is not None:
            other.cancel()
            await asyncio.gather(other, return_exceptions=True)
        await asyncio.gather(task, return_exceptions=True)
        await runtime.database.close()

run(main())
"""
    with worker(client, tmp_path, code=code) as (process, log):
        assert process.wait(timeout=50) == 0, log.read_text(encoding="utf8")
        assert "common_runtime_verified" in log.read_text(encoding="utf8")
    assert client.get(f"/api/v1/flow-runs/{identifier}").json()["status"] == "pending"
    assert client.get(f"/api/v1/model-invocations?flow_run_id={identifier}").json() == []

    async def state():
        return await runtime.flow_engine.client.list_workflows_async(
            workflow_ids=[f"flow:{identifier}", "test:common-runtime:pending"]
        )

    rows = client.portal.call(state)
    assert {row.workflow_id: row.status for row in rows} == {
        f"flow:{identifier}": "ENQUEUED",
        "test:common-runtime:pending": "PENDING",
    }


def test_missing_dbos_schema_stops_worker_even_with_flow_disabled(client, tmp_path):
    # Older installations with Flow disabled may never have initialized DBOS.
    query(client, lambda session: session.execute(text("DROP SCHEMA dbos CASCADE")))
    code = """
import os
os.environ['KEYSTONE_FLOW_ENABLED'] = 'false'
from keystone.entrypoints.worker import main
main()
"""
    with worker(client, tmp_path, code=code) as (process, log):
        assert process.wait(timeout=25) != 0, log.read_text(encoding="utf8")
    assert client.get("/api/v1/worker/status").json()["status"] == "offline"

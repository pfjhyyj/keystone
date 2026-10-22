import asyncio
import hashlib
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from threading import Event
from uuid import UUID

import pytest
from dbos import DBOS
from fastapi import Request
from sqlalchemy import func, select, update
from starlette.requests import ClientDisconnect
from support.local_media_stub import definition
from test_files_dictionary import clean, upload
from test_files_dictionary import client as client
from test_flow import wait_run, worker
from test_identity import bearer, query, role, token

from keystone.core.errors import ServiceError
from keystone.platform.audit.models import AuditLog
from keystone.platform.audit.services import writer as audit
from keystone.platform.dictionary.models import Dictionary
from keystone.platform.files.controllers.streaming import ContentResponse
from keystone.platform.files.models import File, FileGrant, FileReference
from keystone.platform.files.schemas import Grant
from keystone.platform.files.services.files import FileService
from keystone.platform.files.services.grants import FileGrantService
from keystone.platform.files.services.references import FileReferences
from keystone.platform.flow.engine import Engine
from keystone.platform.flow.models import FlowRun
from keystone.platform.flow.schemas import Start
from keystone.platform.flow.services.runs import FlowService
from keystone.platform.identity.dependencies import access_context

pytestmark = pytest.mark.postgres


@pytest.fixture
def media(client, monkeypatch):
    runtime = client.app.state.runtime
    runtime.flow_definitions = {definition.key: definition}
    runtime.settings.file_max_bytes = 16 * 1024**2
    runtime.settings.flow_worker_concurrency = 1
    monkeypatch.setenv("KEYSTONE_FLOW_WORKER_CONCURRENCY", "1")
    DBOS.migrate(runtime.settings.database_url.get_secret_value())
    runtime.flow_engine = Engine(runtime.settings)
    return client


def start(client, identifier, key="local", **inputs):
    return client.post(
        "/api/v1/flow-runs",
        json={
            "definition_key": definition.key,
            "request_key": key,
            "input": {"source_file_id": identifier, **inputs},
        },
    )


def test_head_range_empty_corruption_and_current_authorization(client, monkeypatch):
    file = upload(client, b"0123456789", content_type="video/mp4").json()
    path = f"/api/v1/files/{file['id']}/content"
    head = client.head(path)
    assert head.status_code == 200 and not head.content
    assert head.headers["content-length"] == "10"
    assert head.headers["content-type"] == "video/mp4"
    assert head.headers["content-disposition"].startswith("inline;")
    assert head.headers["accept-ranges"] == "bytes"
    cases = [
        ("0-0", b"0", "0-0"),
        ("7-", b"789", "7-9"),
        ("-3", b"789", "7-9"),
        ("8-99", b"89", "8-9"),
        ("-99", b"0123456789", "0-9"),
    ]
    for value, expected, interval in cases:
        result = client.get(path, headers={"Range": "bytes=" + value})
        assert result.status_code == 206 and result.content == expected
        assert result.headers["content-range"] == f"bytes {interval}/10"
        assert int(result.headers["content-length"]) == len(expected)
    for value in ["bytes=10-", "bytes=4-2", "bytes=-0", "bytes=", "bytes=0-1,4-5", "items=0-1"]:
        response = client.get(path, headers={"Range": value})
        assert response.status_code == 416 and response.headers["content-range"] == "bytes */10"
    assert (
        client.get(path, headers={"Range": "bytes=0-1", "If-Range": '"stale"'}).status_code == 200
    )
    assert client.head(path, headers={"Range": "bytes=0-1"}).headers["content-length"] == "10"
    empty = upload(client, b"", key="empty").json()["id"]
    assert client.get(f"/api/v1/files/{empty}/content").content == b""
    assert (
        client.get(f"/api/v1/files/{empty}/content", headers={"Range": "bytes=0-"}).status_code
        == 416
    )

    # A stable stat signature reuses integrity validation instead of rescanning for every seek.
    import keystone.platform.files.storage as storage_module

    original_hash = storage_module.hashlib.sha256
    scans = 0

    def checksum(*args, **kwargs):
        nonlocal scans
        if not args and not kwargs:
            scans += 1
        return original_hash(*args, **kwargs)

    monkeypatch.setattr(storage_module.hashlib, "sha256", checksum)
    storage = client.app.state.runtime.storage.get()
    assert storage._verified
    count = len(storage._verified)
    assert client.get(path, headers={"Range": "bytes=5-6"}).content == b"56"
    assert scans == 0
    assert len(storage._verified) == count
    (storage.root / "files" / UUID(file["id"]).hex).write_bytes(b"corruption")
    assert client.get(path, headers={"Range": "bytes=0-1"}).status_code == 503
    assert scans == 1

    valid = upload(client, key="revoked").json()
    only_inspect = bearer(token(client, ["file:inspect"])["token"])
    url = f"/api/v1/files/{valid['id']}/content"
    assert client.head(url, headers=only_inspect).status_code == 403
    grant = next(
        g
        for g in client.get(f"/api/v1/files/{valid['id']}/grants").json()
        if g["action"] == "content_read"
    )
    client.delete(f"/api/v1/files/{valid['id']}/grants/{grant['id']}")
    assert client.get(url, headers={"Range": "bytes=0-1"}).status_code == 404
    assert client.head(url).status_code == 404


def test_stream_disconnect_shared_readers_and_delete_coordination(client):
    identifier = upload(client).json()["id"]
    runtime = client.app.state.runtime

    @client.app.get("/_acceptance/disconnect")
    async def disconnect(request: Request):
        service = FileService(access_context(request), runtime.storage)
        scope = {**request.scope, "asgi": {"spec_version": "2.4"}}

        async def send(message):
            if message["type"] == "http.response.body":
                raise OSError("client disconnected")

        with pytest.raises(ClientDisconnect):
            await ContentResponse(service.open_content(UUID(identifier)), request)(
                scope, request.receive, send
            )
        # Two shared readers coexist, and no exclusive writer/cleaner can acquire their lease.
        async with service.open_content(UUID(identifier)), service.open_content(UUID(identifier)):
            with pytest.raises(ServiceError) as busy:
                with runtime.storage.lease(UUID(identifier)):
                    pytest.fail("Writer entered while reading")
            assert busy.value.code == "file_busy"
        return {"closed": True}

    assert client.get("/_acceptance/disconnect").json() == {"closed": True}
    with ExitStack() as stack:
        for _ in range(runtime.settings.file_transfer_slots):
            stack.enter_context(runtime.storage.transfers.slot())
        response = client.get(f"/api/v1/files/{identifier}/content")
        assert response.status_code == 503 and response.json()["code"] == "file_transfer_busy"
    assert client.delete(f"/api/v1/files/{identifier}").status_code == 202
    clean(client)
    assert client.get(f"/api/v1/files/{identifier}").json()["status"] == "deleted"


def test_disconnect_before_headers_cancels_integrity_scan(client, monkeypatch):
    file_id = UUID(upload(client).json()["id"])
    runtime = client.app.state.runtime
    entered = Event()
    original = runtime.storage.get().open_local

    def paused(*args):
        entered.set()
        # Receive reports disconnect while verification is still being prepared.
        assert args[-1].wait(5)
        return original(*args)

    monkeypatch.setattr(runtime.storage.get(), "open_local", paused)

    @client.app.get("/_acceptance/disconnect-before-headers")
    async def disconnect(request: Request):
        async def receive():
            assert await asyncio.to_thread(entered.wait, 5)
            return {"type": "http.disconnect"}

        async def send(message):
            pytest.fail("Headers were sent after disconnect")

        context = FileService(access_context(request), runtime.storage).open_content(file_id)
        await ContentResponse(context, request)(request.scope, receive, send)
        return {"closed": True}

    assert client.get("/_acceptance/disconnect-before-headers").json() == {"closed": True}
    with ExitStack() as stack:
        stack.enter_context(runtime.storage.lease(file_id))
        for _ in range(runtime.settings.file_transfer_slots):
            stack.enter_context(runtime.storage.transfers.slot())


def test_legacy_read_rejects_large_file_before_allocating(client, monkeypatch):
    file_id = UUID(upload(client).json()["id"])
    runtime = client.app.state.runtime
    query(
        client,
        lambda session: session.execute(
            update(File).where(File.id == file_id).values(size_bytes=65 * 1024**2)
        ),
    )

    def unexpected_read(*args):
        pytest.fail("Large file was read into memory")

    monkeypatch.setattr(runtime.storage.get(), "read", unexpected_read)

    @client.app.get("/_acceptance/bounded-read")
    async def read(request: Request):
        await FileService(access_context(request), runtime.storage).content(file_id)

    assert client.get("/_acceptance/bounded-read").status_code == 413


@pytest.mark.parametrize("credential", ["session", "pat"])
@pytest.mark.parametrize("failure", ["business", "audit"])
def test_composed_transaction_rollback_idempotency_and_audit(
    media, credential, failure, monkeypatch
):
    client = media
    identifier = upload(client).json()["id"]
    target_role = role(client, "test-readers", ["file:content_read"])["id"]
    credential_id = None
    if credential == "pat":
        issued = token(
            client,
            [
                "flow:start",
                "flow:read",
                "file:manage",
                "file:create",
                "file:inspect",
                "file:content_read",
            ],
        )
        client.headers.update(bearer(issued["token"]))
        credential_id = issued["id"]
    runtime = client.app.state.runtime

    @client.app.post("/_acceptance/compose")
    async def compose(request: Request, fail: bool = False, different: bool = False):
        context = access_context(request)
        async with context.access(action="flow.start", target="flow.run") as (session, auth):
            record = await session.scalar(select(Dictionary).where(Dictionary.key == "acceptance"))
            if record is None:
                record = Dictionary(
                    key="acceptance", name="Atomic fixture", created_by=auth.user.id
                )
                session.add(record)
                await session.flush()
                await context.log(
                    session,
                    auth,
                    "dictionary.definition.create",
                    "dictionary",
                    record.id,
                    {"key": record.key, "name": record.name},
                )
            await FileGrantService(context).grant_in_transaction(
                session,
                auth,
                UUID(identifier),
                Grant(subject_type="role", role_id=UUID(target_role), action="content_read"),
            )
            await FileReferences(session, auth, "acceptance").replace(
                str(record.id), "source", [identifier]
            )
            result = await FlowService(context, runtime.flow_definitions).start_in_transaction(
                session,
                auth,
                Start(
                    definition_key=definition.key,
                    request_key="atomic",
                    input={
                        "source_file_id": identifier,
                        "wait_for": "changed" if different else None,
                    },
                ),
            )
            if fail:
                raise RuntimeError("Rollback all facts")
            return result

    if failure == "business":
        assert client.post("/_acceptance/compose?fail=true").status_code == 500
    else:
        original = audit.record

        async def unavailable(session, action, *args, **kwargs):
            if action == "flow.start" and kwargs.get("outcome", "success") == "success":
                raise RuntimeError("Success audit failed")
            return await original(session, action, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(audit, "record", unavailable)
            assert client.post("/_acceptance/compose").status_code == 500
    for model in (Dictionary, FlowRun, FileReference):
        assert (
            query(
                client,
                lambda session, model=model: session.scalar(
                    select(func.count()).select_from(model)
                ),
            )
            == 0
        )
    assert (
        query(client, lambda session: session.scalar(select(func.count()).select_from(FileGrant)))
        == 3
    )
    assert (
        query(
            client,
            lambda session: session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(
                    AuditLog.action.in_(
                        [
                            "flow.start",
                            "file.grant",
                            "file.reference",
                            "dictionary.definition.create",
                        ]
                    ),
                    AuditLog.outcome == "success",
                )
            ),
        )
        == 0
    )
    result = client.post("/_acceptance/compose").json()
    assert result["status"] == "pending"
    assert client.post("/_acceptance/compose").json()["id"] == result["id"]
    assert (
        client.post("/_acceptance/compose?different=true").json()["code"] == "idempotency_conflict"
    )
    audits = query(
        client,
        lambda session: session.scalars(
            select(AuditLog).where(AuditLog.action == "flow.start", AuditLog.outcome == "success")
        ),
    ).all()
    assert len(audits) == 1 and str(audits[0].actor_id) == result["actor_id"]
    if credential_id:
        assert audits[0].details["credential_id"] == credential_id


@pytest.mark.parametrize("mode", ["normal", "cancel", "cancel_engine_unavailable", "crash"])
def test_model_free_worker_materialization_heartbeat_cancel_and_restart(media, tmp_path, mode):
    client = media
    data = b"\x00\xfflocal media\x81" * 2000
    identifier = upload(client, data).json()["id"]
    gate = tmp_path / "continue"
    run_id = start(client, identifier, wait_for=str(gate)).json()["id"]
    code = "from support.local_media_stub import install; install(); "
    if mode == "cancel_engine_unavailable":
        code = "import os; os.environ['KEYSTONE_ACCEPTANCE_CANCEL_FAIL']='1'; " + code
    if mode == "crash":
        crash_code = "import os; os.environ['KEYSTONE_ACCEPTANCE_CRASH']='1'; " + code
        with worker(
            client,
            tmp_path,
            code=crash_code + "from keystone.entrypoints.worker import main; main()",
            stub=False,
        ) as (process, log):
            process.wait(timeout=30)
            assert process.returncode == 71, log.read_text(encoding="utf8")
        assert client.get(f"/api/v1/flow-runs/{run_id}").json()["status"] not in {
            "failed",
            "cancelled",
        }
        gate.touch()
    with worker(
        client,
        tmp_path,
        code=code + "from keystone.entrypoints.worker import main; main()",
        stub=False,
    ) as (process, log):
        deadline = time.monotonic() + 30
        while not gate.with_suffix(".entered").exists():
            assert process.poll() is None and time.monotonic() < deadline, log.read_text(
                encoding="utf8"
            )
            time.sleep(0.1)
        if mode != "crash":
            first = client.get("/api/v1/worker/status").json()
            deadline = time.monotonic() + 12
            while True:
                current = client.get("/api/v1/worker/status").json()
                if (
                    current["status"] == "ready"
                    and current["last_heartbeat_at"] != first["last_heartbeat_at"]
                ):
                    break
                assert process.poll() is None and time.monotonic() < deadline
                time.sleep(0.1)
        cancelled = mode.startswith("cancel")
        if cancelled:
            client.post(f"/api/v1/flow-runs/{run_id}/cancel")
        else:
            gate.touch()
        done = wait_run(client, run_id, process, log)
        assert done["status"] == ("cancelled" if cancelled else "succeeded"), (
            done,
            log.read_text(encoding="utf8"),
        )
        if not cancelled:
            result_id = done["result"]["file_id"]
            response = client.get(f"/api/v1/files/{result_id}/content")
            assert hashlib.sha256(response.content).digest() == hashlib.sha256(data).digest()
            assert len(client.get(f"/api/v1/files/{result_id}/references").json()) == 2
        directory = client.app.state.runtime.settings.file_coordination_root / ".transfers"
        deadline = time.monotonic() + 5
        while list(directory.iterdir()):
            assert time.monotonic() < deadline
            time.sleep(0.1)
    assert client.get("/api/v1/model-invocations").json() == []


def test_worker_admission_and_size_limits_precede_storage_scan(media, monkeypatch, tmp_path):
    from keystone.platform.flow import host

    runtime = media.app.state.runtime
    identifier = upload(media).json()["id"]
    run_id = start(media, identifier).json()["id"]
    monkeypatch.setattr(host, "_runtime", runtime)

    def unexpected_scan(*args):
        pytest.fail("Storage scanned before admission/size checks")

    monkeypatch.setattr(runtime.storage.get(), "open_local", unexpected_scan)

    async def materialize():
        async with host.materialize_file(run_id, identifier):
            pytest.fail("Oversized or full-capacity materialization accepted")

    with ExitStack() as stack:
        for _ in range(runtime.settings.file_transfer_slots):
            stack.enter_context(runtime.storage.transfers.slot())
        with pytest.raises(ServiceError) as rejected:
            media.portal.call(materialize)
        assert rejected.value.code == "file_transfer_busy"
        with pytest.raises(ServiceError) as rejected:
            media.portal.call(
                lambda: host.upload_file(
                    run_id, "copy", tmp_path / "missing", name="copy.mp4", content_type="video/mp4"
                )
            )
        assert rejected.value.code == "file_transfer_busy"
    runtime.settings.file_temp_bytes = runtime.settings.file_transfer_slots * 4
    with pytest.raises(ServiceError) as rejected:
        media.portal.call(materialize)
    assert rejected.value.code == "file_too_large"


def test_upload_waiting_for_api_slot_obeys_timeout(client):
    runtime = client.app.state.runtime
    runtime.settings.file_upload_timeout_seconds = 1
    client.app.state.upload_slots = asyncio.Semaphore(0)
    result = upload(client)
    assert result.status_code == 408 and result.json()["code"] == "upload_timeout"
    assert client.get("/api/v1/files").json() == []
    client.app.state.upload_slots = asyncio.Semaphore(1)
    assert upload(client).status_code == 201


def test_timeout_during_final_write_keeps_retryable_file(client, monkeypatch):
    runtime = client.app.state.runtime
    runtime.settings.file_upload_timeout_seconds = 1
    storage = runtime.storage.get()
    original = storage.write
    entered, release = Event(), Event()
    staged = []

    def paused(*args):
        staged.append(args[-1])
        entered.set()
        assert release.wait(5)
        return original(*args)

    monkeypatch.setattr(storage, "write", paused)
    with ThreadPoolExecutor() as pool:
        pending = pool.submit(upload, client)
        try:
            assert entered.wait(5)
            assert staged[0].cancelled.wait(3)
        finally:
            release.set()
        assert pending.result().status_code == 408
    file = client.get("/api/v1/files").json()[0]
    assert file["status"] == "failed"
    assert client.get(f"/api/v1/files/{file['id']}/content").status_code == 409
    assert not list((runtime.settings.file_coordination_root / ".transfers").iterdir())
    monkeypatch.setattr(storage, "write", original)
    retried = upload(client).json()
    assert retried["id"] == file["id"] and retried["status"] == "active"


def test_worker_capacity_queues_then_cancel_releases_next_run(media, tmp_path):
    client = media
    identifier = upload(client).json()["id"]
    first_gate, second_gate = tmp_path / "first", tmp_path / "second"
    first = start(client, identifier, key="first", wait_for=str(first_gate)).json()["id"]
    code = (
        "from support.local_media_stub import install; install(); "
        "from keystone.entrypoints.worker import main; main()"
    )
    with worker(client, tmp_path, code=code, stub=False) as (process, log):
        deadline = time.monotonic() + 30
        while not first_gate.with_suffix(".entered").exists():
            assert process.poll() is None and time.monotonic() < deadline
            time.sleep(0.1)
        second = start(client, identifier, key="second", wait_for=str(second_gate)).json()["id"]
        time.sleep(1)
        assert not second_gate.with_suffix(".entered").exists()
        client.post(f"/api/v1/flow-runs/{first}/cancel")
        assert wait_run(client, first, process, log)["status"] == "cancelled"
        deadline = time.monotonic() + 20
        while not second_gate.with_suffix(".entered").exists():
            assert process.poll() is None and time.monotonic() < deadline
            time.sleep(0.1)
        second_gate.touch()
        assert wait_run(client, second, process, log)["status"] == "succeeded"

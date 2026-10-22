"""Trusted reference release and deletion acceptance share the caller's SQL transaction."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import UUID, uuid4

import pytest
from sqlalchemy import update
from test_file_storages import multi_client as multi_client
from test_file_storages import s3 as s3
from test_files_dictionary import add_grant, clean, upload
from test_files_dictionary import client as client
from test_identity import (
    NEW_PASSWORD,
    PASSWORD,
    assign,
    create_user,
    login,
    query,
    role,
    token,
)

from keystone.core.errors import ServiceError
from keystone.platform.audit.services import writer as audit
from keystone.platform.files.models import File
from keystone.platform.files.services.files import FileService
from keystone.platform.files.services.references import FileReferences, FileReferenceService
from keystone.platform.identity.access import AccessContext
from keystone.platform.identity.policy import Credential

pytestmark = pytest.mark.postgres


def context(client, secret=None):
    rt = client.app.state.runtime
    return AccessContext(
        rt.database,
        rt.settings,
        Credential("pat", secret)
        if secret
        else Credential("session", client.cookies.get("keystone_session")),
        correlation_id="delete-transaction",
    )


def register(client, claims):
    identity = context(client)

    async def operation():
        async with identity.access() as (session, auth):
            grouped = {}
            for owner, slot, identifier in claims:
                grouped.setdefault((owner, slot), []).append(identifier)
            for (owner, slot), identifiers in grouped.items():
                namespace, key = owner.split(":", 1)
                await FileReferences(session, auth, namespace).replace(key, slot, identifiers)

    client.portal.call(operation)


def compose(client, identifiers=(), claims=(), *, identity=None, rollback=False):
    identity = identity or context(client)
    service = FileService(identity, client.app.state.runtime.storage)

    async def operation():
        async with identity.access() as (session, auth):
            removed = await FileReferenceService(identity).release_in_transaction(
                session, auth, claims
            )
            for identifier in sorted(identifiers):
                await service.request_delete_in_transaction(session, auth, UUID(str(identifier)))
            if rollback:
                raise RuntimeError("business rollback")
            return removed

    return client.portal.call(operation)


def references(client, identifier):
    return client.get(f"/api/v1/files/{identifier}/references").json()


def events(client, identifier, action):
    return client.get(
        "/api/v1/audit-logs", params={"target_id": identifier, "action": action}
    ).json()


def test_release_uses_all_three_exact_keys_and_audits_only_real_changes(client):
    first, second = upload(client).json(), upload(client, key="second").json()
    claim = ("orders:one", "result", first["id"])
    retained = [
        ("orders:one:child", "result", first["id"]),
        ("orders:one", "input", first["id"]),
        ("orders:one", "result", second["id"]),
    ]
    register(client, [claim, *retained])
    # A trusted business can release usage without reading or managing file content.
    identity = context(client, token(client, ["file:inspect"])["token"])
    assert (
        compose(
            client,
            claims=[claim, claim, ("orders:absent", "result", first["id"])],
            identity=identity,
        )
        == 1
    )
    assert compose(client, claims=[claim], identity=identity) == 0
    assert references(client, first["id"]) == [
        {"owner": "orders:one", "slot": "input"},
        {"owner": "orders:one:child", "slot": "result"},
    ]
    assert references(client, second["id"]) == [{"owner": "orders:one", "slot": "result"}]
    logs = [r for r in events(client, first["id"], "file.reference") if not r["details"]["added"]]
    assert len(logs) == 1
    assert logs[0]["actor_type"] == "user" and logs[0]["actor_id"] == first["owner_id"]
    assert logs[0]["correlation_id"] == "delete-transaction"
    assert logs[0]["details"].keys() == {"owner", "slot", "added", "credential_id"}
    assert client.get(f"/api/v1/files/{first['id']}").json()["status"] == "active"
    assert client.delete(f"/api/v1/files/{first['id']}").json()["code"] == "file_referenced"


def test_release_and_acceptance_retain_input_bytes_until_existing_maintenance_runs(
    client, monkeypatch
):
    target, source = upload(client).json(), upload(client, key="input").json()
    claims = [("orders:one", "result", target["id"]), ("orders:one", "input", source["id"])]
    register(client, claims)
    rt = client.app.state.runtime
    original = rt.storage.get().delete

    def unexpected(*args):
        pytest.fail("Native storage I/O inside the business transaction")

    monkeypatch.setattr(rt.storage.get(), "delete", unexpected)
    assert compose(client, [target["id"]], claims) == 2
    assert compose(client, [target["id"]], claims) == 0
    assert client.get(f"/api/v1/files/{target['id']}").json()["status"] == "deleting"
    assert len(client.get(f"/api/v1/files/{target['id']}/grants").json()) == 3
    assert len(events(client, target["id"], "file.delete.request")) == 1
    assert client.get(f"/api/v1/files/{source['id']}/content").content == b"hello"
    assert not references(client, target["id"]) and not references(client, source["id"])
    monkeypatch.setattr(rt.storage.get(), "delete", original)
    clean(client)
    assert client.get(f"/api/v1/files/{target['id']}").json()["status"] == "deleted"
    assert client.get(f"/api/v1/files/{target['id']}/grants").json() == []
    assert compose(client, [target["id"]], claims) == 0
    assert len(events(client, target["id"], "file.delete.request")) == 1


@pytest.mark.parametrize("failure", ["business", "file.reference", "file.delete.request"])
def test_business_and_success_audit_failures_roll_back_the_entire_batch(
    client, monkeypatch, failure
):
    files = [upload(client, key=str(i)).json() for i in range(2)]
    claims = [("orders:one", "result", f["id"]) for f in files]
    register(client, claims)
    record = audit.record

    async def fail(session, action, *args, **kwargs):
        if action == failure:
            raise RuntimeError("audit unavailable")
        await record(session, action, *args, **kwargs)

    monkeypatch.setattr(audit, "record", fail)
    with pytest.raises(RuntimeError):
        compose(client, [f["id"] for f in files], claims, rollback=failure == "business")
    for file in files:
        assert client.get(f"/api/v1/files/{file['id']}").json()["status"] == "active"
        assert references(client, file["id"]) == [{"owner": "orders:one", "slot": "result"}]
        assert not events(client, file["id"], "file.delete.request")
        assert all(r["details"]["added"] for r in events(client, file["id"], "file.reference"))


@pytest.mark.parametrize(
    "retained", [("other:hold", "result"), ("orders:one", "input"), ("orders:one:child", "result")]
)
def test_undeclared_references_block_deletion_and_rollback_approved_releases(client, retained):
    identifier = upload(client).json()["id"]
    claim = ("orders:one", "result", identifier)
    register(client, [claim, (*retained, identifier)])
    with pytest.raises(ServiceError) as rejected:
        compose(client, [identifier], [claim])
    assert rejected.value.code == "file_referenced"
    assert len(references(client, identifier)) == 2
    assert not events(client, identifier, "file.delete.request")


@pytest.mark.parametrize("status", ["active", "disabled", "uploading", "failed"])
def test_transaction_entry_preserves_http_deletion_lifecycle(client, status):
    identifier = upload(client).json()["id"]
    query(
        client,
        lambda s: s.execute(update(File).where(File.id == UUID(identifier)).values(status=status)),
    )
    compose(client, [identifier])
    assert client.get(f"/api/v1/files/{identifier}").json()["status"] == "deleting"
    assert client.delete(f"/api/v1/files/{identifier}").status_code == 202
    assert len(events(client, identifier, "file.delete.request")) == 1


def test_narrow_or_revoked_pat_cannot_accept_deletion(client):
    identifier = upload(client).json()["id"]
    issued = token(client, ["file:inspect"])
    with pytest.raises(ServiceError) as rejected:
        compose(client, [identifier], identity=context(client, issued["token"]))
    assert rejected.value.status == 403
    assert client.delete(f"/api/v1/auth/tokens/{issued['id']}").status_code == 204
    with pytest.raises(ServiceError) as rejected:
        compose(client, [identifier], identity=context(client, issued["token"]))
    assert rejected.value.status == 401
    assert client.get(f"/api/v1/files/{identifier}").json()["status"] == "active"


def test_late_object_denial_rolls_back_prior_file_marks_and_released_references(client):
    files = sorted([upload(client, key=str(i)).json() for i in range(2)], key=lambda f: f["id"])
    claims = [("orders:one", "result", f["id"]) for f in files]
    register(client, claims)
    user = create_user(client)
    group = role(client, "manager", ["file:manage"])
    assign(client, user["id"], group["id"])
    assert (
        add_grant(
            client, files[0]["id"], "manage", subject_type="user", user_id=user["id"]
        ).status_code
        == 201
    )
    login(client, "alice")
    assert (
        client.post(
            "/api/v1/auth/password", json={"current_password": PASSWORD, "password": NEW_PASSWORD}
        ).status_code
        == 204
    )
    login(client, "alice", NEW_PASSWORD)
    identity = context(client, token(client, ["file:manage"])["token"])
    with pytest.raises(ServiceError) as rejected:
        compose(client, [f["id"] for f in files], claims, identity=identity)
    assert rejected.value.code == "file_not_found"
    login(client)
    for file in files:
        assert client.get(f"/api/v1/files/{file['id']}").json()["status"] == "active"
        assert references(client, file["id"])
        assert not events(client, file["id"], "file.delete.request")
    # Successful deletion needs a current object manage grant, even for a previously used PAT.
    grant = next(
        g
        for g in client.get(f"/api/v1/files/{files[0]['id']}/grants").json()
        if g["user_id"] == user["id"]
    )
    assert client.delete(f"/api/v1/files/{files[0]['id']}/grants/{grant['id']}").status_code == 204
    with pytest.raises(ServiceError) as rejected:
        compose(client, [files[0]["id"]], [claims[0]], identity=identity)
    assert rejected.value.code == "file_not_found"


@pytest.mark.parametrize(
    "owner,slot,identifier",
    [
        ("", "slot", None),
        ("o" * 161, "slot", None),
        ("o", "s" * 101, None),
        ("o", "slot", "bad-id"),
    ],
)
def test_invalid_claims_are_rejected_before_any_release(client, owner, slot, identifier):
    file_id = upload(client).json()["id"]
    claim = ("orders:one", "result", file_id)
    register(client, [claim])
    with pytest.raises(ValueError):
        compose(client, claims=[claim, (owner, slot, identifier or file_id)])
    assert references(client, file_id) == [{"owner": "orders:one", "slot": "result"}]


def test_missing_file_cannot_partially_release_another_file(client):
    identifier = upload(client).json()["id"]
    claim = ("orders:one", "result", identifier)
    register(client, [claim])
    with pytest.raises(ServiceError) as rejected:
        compose(client, claims=[claim, ("orders:missing", "result", str(uuid4()))])
    assert rejected.value.code == "file_not_found"
    assert references(client, identifier)


def test_cancellation_before_commit_rolls_back_references_and_deletion_mark(client):
    identifier = upload(client).json()["id"]
    claim = ("orders:one", "result", identifier)
    register(client, [claim])
    identity = context(client)

    async def operation():
        entered = asyncio.Event()

        async def transaction():
            async with identity.access() as (session, auth):
                await FileReferenceService(identity).release_in_transaction(session, auth, [claim])
                await FileService(
                    identity, client.app.state.runtime.storage
                ).request_delete_in_transaction(session, auth, UUID(identifier))
                entered.set()
                await asyncio.Future()

        task = asyncio.create_task(transaction())
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    client.portal.call(operation)
    assert client.get(f"/api/v1/files/{identifier}").json()["status"] == "active"
    assert references(client, identifier) == [{"owner": "orders:one", "slot": "result"}]
    assert not events(client, identifier, "file.delete.request")


def test_accepted_deletion_waits_for_existing_native_read_lease_in_maintenance(client):
    identifier = upload(client).json()["id"]
    rt = client.app.state.runtime
    path = rt.storage.get().root / "files" / UUID(identifier).hex
    with rt.storage.lease(UUID(identifier), shared=True):
        compose(client, [identifier])
        clean(client)
        assert path.read_bytes() == b"hello"
        assert client.get(f"/api/v1/files/{identifier}").json()["status"] == "deleting"
    clean(client)
    assert not path.exists()
    assert client.get(f"/api/v1/files/{identifier}").json()["status"] == "deleted"


def test_reverse_order_batches_serialize_and_keep_locks_until_commit(client, monkeypatch):
    files = [upload(client, key=str(i)).json() for i in range(2)]
    claims = [("orders:one", "result", f["id"]) for f in files]
    register(client, claims)
    entered, release, second_started = Event(), Event(), Event()
    original = audit.record

    async def pause(session, action, *args, **kwargs):
        if (
            action == "file.reference"
            and kwargs.get("details", {}).get("added") is False
            and not entered.is_set()
        ):
            entered.set()
            assert await asyncio.to_thread(release.wait, 10)
        await original(session, action, *args, **kwargs)

    def second():
        second_started.set()
        return compose(client, [f["id"] for f in files], list(reversed(claims)))

    monkeypatch.setattr(audit, "record", pause)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(compose, client, [f["id"] for f in files], claims)
        try:
            assert entered.wait(5)
            pending = pool.submit(second)
            assert second_started.wait(5)
            with pytest.raises(TimeoutError):
                pending.result(timeout=0.2)
        finally:
            release.set()
        assert first.result(timeout=10) == 2
        assert pending.result(timeout=10) == 0
    for file in files:
        assert len(events(client, file["id"], "file.delete.request")) == 1


def test_new_reference_cannot_race_accepted_deletion(client, monkeypatch):
    identifier = upload(client).json()["id"]
    claim = ("orders:one", "result", identifier)
    register(client, [claim])
    entered, release, writer_started = Event(), Event(), Event()
    original = audit.record

    async def pause(session, action, *args, **kwargs):
        if action == "file.delete.request":
            entered.set()
            assert await asyncio.to_thread(release.wait, 10)
        await original(session, action, *args, **kwargs)

    def writer():
        writer_started.set()
        register(client, [("other:late", "input", identifier)])

    monkeypatch.setattr(audit, "record", pause)
    with ThreadPoolExecutor(2) as pool:
        deletion = pool.submit(compose, client, [identifier], [claim])
        try:
            assert entered.wait(5)
            pending = pool.submit(writer)
            assert writer_started.wait(5)
            with pytest.raises(TimeoutError):
                pending.result(timeout=0.2)
        finally:
            release.set()
        assert deletion.result(timeout=10) == 1
        with pytest.raises(ServiceError) as rejected:
            pending.result(timeout=10)
        assert rejected.value.code == "file_unavailable"
    assert not references(client, identifier)


def test_s3_acceptance_stays_sql_only_and_existing_maintenance_deletes_exact_object(
    multi_client, s3
):
    client = multi_client
    file = upload(client, storage_key="cloud").json()
    claim = ("orders:one", "result", file["id"])
    register(client, [claim])
    assert s3.objects
    assert compose(client, [file["id"]], [claim]) == 1
    assert s3.objects and len(client.get(f"/api/v1/files/{file['id']}/grants").json()) == 3
    clean(client)
    assert not s3.objects
    assert client.get(f"/api/v1/files/{file['id']}").json()["status"] == "deleted"

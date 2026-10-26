"""Generic maintenance and local lookup, with real grants, transactions and leases."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select, update
from test_files_dictionary import clean, upload
from test_files_dictionary import client as client
from test_identity import query, token

from keystone.core.errors import ServiceError
from keystone.platform.audit.models import AuditLog
from keystone.platform.audit.services import writer as audit
from keystone.platform.files.models import File, FileGrant, FileReference
from keystone.platform.files.services import access as policy
from keystone.platform.files.services.files import FileService
from keystone.platform.files.services.references import FileReferences, FileReferenceService
from keystone.platform.identity.models import User
from keystone.platform.identity.schemas.credentials import Credential
from keystone.platform.identity.services.access import AccessContext
from keystone.platform.identity.services.authorization import identity_lock, load_authorization

pytestmark = pytest.mark.postgres


def file_service(client, credential=None):
    rt = client.app.state.runtime
    credential = credential or Credential("session", client.cookies.get("keystone_session"))
    return FileService(AccessContext(rt.database, rt.settings, credential), rt.storage)


def bind(client, owner_id, claims):
    async def apply(session):
        await identity_lock(session)
        auth = await load_authorization(session, owner_id)
        for owner, slot, identifier in sorted(claims, key=lambda c: c[2]):
            namespace, _, key = owner.partition(":")
            await FileReferences(session, auth, namespace).replace(key, slot, [identifier])

    query(client, apply)


def fixture(client):
    owner = UUID(client.get("/api/v1/auth/me").json()["user"]["id"])
    original, first, second = [
        UUID(upload(client, key=key).json()["id"]) for key in ("original", "first", "second")
    ]
    claims = {
        ("example:expired", "input", original),
        ("example:expired", "output", first),
        ("flow:ended", "artifact.part", second),
    }
    bind(client, owner, claims)
    return owner, original, {first, second}, claims


def facts(client):
    async def read(session):
        return (
            list(
                (
                    await session.execute(
                        select(File.id, File.status, File.updated_by).order_by(File.id)
                    )
                ).all()
            ),
            list(
                (
                    await session.execute(
                        select(
                            FileReference.owner, FileReference.slot, FileReference.file_id
                        ).order_by(FileReference.owner, FileReference.slot, FileReference.file_id)
                    )
                ).all()
            ),
            await session.scalar(select(func.count()).select_from(AuditLog)),
        )

    return query(client, read)


def retire(client, owner, claims, artifacts):
    rt = client.app.state.runtime

    async def apply():
        service = FileReferenceService(None)
        approved_claims, approved_artifacts = service.validate_retirement_scope(claims, artifacts)
        with ExitStack() as leases:
            for identifier in sorted(approved_artifacts):
                leases.enter_context(rt.storage.lease(identifier))
            async with rt.database.transaction() as session:
                return await service.retire_in_transaction(
                    session,
                    references=approved_claims,
                    artifacts=approved_artifacts,
                    owner_id=owner,
                )

    return client.portal.call(apply)


def test_retirement_defers_whole_group_and_rolls_back_before_recovering(client, monkeypatch):
    owner, original, artifacts, claims = fixture(client)
    first = min(artifacts)
    bind(client, owner, {("other:kept", "input", original), ("other:kept", "output", first)})
    before = facts(client)
    assert retire(client, owner, claims, artifacts) is False
    assert facts(client) == before
    query(
        client,
        lambda session: session.execute(
            delete(FileReference).where(
                FileReference.owner == "other:kept", FileReference.slot == "output"
            )
        ),
    )
    before = facts(client)
    assert retire(client, uuid4(), claims, artifacts) is False
    assert facts(client) == before
    with client.app.state.runtime.storage.lease(first, shared=True):
        with pytest.raises(ServiceError) as busy:
            retire(client, owner, claims, artifacts)
        assert busy.value.code == "file_busy"
    assert facts(client) == before
    record = audit.record

    async def fail(session, action, *args, **kwargs):
        if action == "file.delete.request":
            raise RuntimeError("isolated retirement audit failure")
        return await record(session, action, *args, **kwargs)

    monkeypatch.setattr(audit, "record", fail)
    with pytest.raises(RuntimeError, match="retirement audit"):
        retire(client, owner, claims, artifacts)
    assert facts(client) == before
    monkeypatch.setattr(audit, "record", record)
    query(
        client,
        lambda session: session.execute(
            update(User).where(User.id == owner).values(is_active=False)
        ),
    )
    assert retire(client, owner, claims, artifacts) is True
    accepted = facts(client)
    states = {identifier: status for identifier, status, _ in accepted[0]}
    assert states[original] == "active" and all(states[i] == "deleting" for i in artifacts)
    assert accepted[1] == [("other:kept", "input", original)]
    # A crash/lost receipt does not restore a deleted reference or add more audits.
    assert retire(client, owner, claims, artifacts) is True
    assert facts(client) == accepted

    async def failed_completion(session, action, *args, **kwargs):
        if action == "file.delete.complete":
            raise RuntimeError("isolated completion failure")
        return await record(session, action, *args, **kwargs)

    monkeypatch.setattr(audit, "record", failed_completion)
    with pytest.raises(RuntimeError, match="completion failure"):
        clean(client)
    assert facts(client) == accepted
    monkeypatch.setattr(audit, "record", record)
    clean(client)
    states = {identifier: status for identifier, status, _ in facts(client)[0]}
    assert states[original] == "active" and all(states[i] == "deleted" for i in artifacts)
    for identifier in artifacts:
        assert not query(
            client,
            lambda session, identifier=identifier: session.scalar(
                select(FileGrant.id).where(FileGrant.file_id == identifier).limit(1)
            ),
        )
    logs = query(
        client,
        lambda session: session.execute(
            select(AuditLog.actor_type, AuditLog.actor_id).where(
                AuditLog.action == "file.delete.request"
            )
        ),
    )
    assert list(logs) == [("system", None), ("system", None)]


def test_retirement_requires_live_artifact_proof_and_preserves_ordinary_delete_auth(client):
    owner, _, artifacts, claims = fixture(client)
    projection = query(
        client,
        lambda session: FileReferenceService(None).claims_in_transaction(
            session, {"example:expired", "flow:ended"}
        ),
    )
    assert set(projection) == claims
    assert (
        query(
            client,
            lambda session: FileReferenceService(None).claims_in_transaction(
                session, {"other:absent"}
            ),
        )
        == []
    )
    with pytest.raises(ServiceError) as oversized:
        query(
            client,
            lambda session: FileReferenceService(None).claims_in_transaction(
                session, {f"example:{i}" for i in range(501)}
            ),
        )
    assert oversized.value.code == "file_reference_limit"
    orphan = UUID(upload(client, key="unclaimed").json()["id"])
    before = facts(client)
    assert retire(client, owner, claims, artifacts | {orphan}) is False
    assert facts(client) == before
    # No maintenance capability appears in the HTTP API or a caller's PAT rights.
    from test_identity import bearer

    limited = bearer(token(client, ["file:inspect"])["token"])
    assert client.delete(f"/api/v1/files/{orphan}", headers=limited).status_code == 403
    assert client.post(f"/api/v1/files/{orphan}/retire", json={}).status_code == 404


def test_new_reference_cannot_race_accepted_retirement(client, monkeypatch):
    owner, _, artifacts, claims = fixture(client)
    identifier = min(artifacts)
    rt = client.app.state.runtime

    async def competition():
        accepted, release, attempting = asyncio.Event(), asyncio.Event(), asyncio.Event()
        locked = policy.locked

        async def observe(session, selected):
            if asyncio.current_task().get_name() == "new-reference":
                attempting.set()
            return await locked(session, selected)

        monkeypatch.setattr(policy, "locked", observe)

        async def deletion():
            with ExitStack() as leases:
                for selected in sorted(artifacts):
                    leases.enter_context(rt.storage.lease(selected))
                async with rt.database.transaction() as session:
                    assert await FileReferenceService(None).retire_in_transaction(
                        session, references=claims, artifacts=artifacts, owner_id=owner
                    )
                    accepted.set()
                    await release.wait()

        async def addition():
            await accepted.wait()
            async with rt.database.transaction() as session:
                await identity_lock(session)
                auth = await load_authorization(session, owner)
                await FileReferences(session, auth, "other").replace("new", "output", [identifier])

        deleting = asyncio.create_task(deletion())
        adding = asyncio.create_task(addition(), name="new-reference")
        try:
            await asyncio.wait_for(attempting.wait(), 5)
            assert not adding.done()
        finally:
            release.set()
        await deleting
        with pytest.raises(ServiceError) as rejected:
            await adding
        assert rejected.value.code == "file_unavailable"

    client.portal.call(competition)
    assert not any(owner == "other:new" for owner, _, _ in facts(client)[1])


@pytest.mark.parametrize(
    "permissions,allowed",
    [
        (["file:inspect"], False),
        (["file:content_read"], False),
        (["file:inspect", "file:content_read"], True),
    ],
)
def test_local_origin_requires_current_functional_and_content_grants(client, permissions, allowed):
    file = upload(client).json()
    identifier = UUID(file["id"])
    service = file_service(client, Credential("pat", token(client, permissions)["token"]))
    if not allowed:
        with pytest.raises(ServiceError) as denied:
            client.portal.call(service.local_origin, identifier)
        assert denied.value.status == 403
        return
    result = client.portal.call(service.local_origin, identifier)
    assert Path(result["path"]).read_bytes() == b"hello"
    assert result["size_bytes"] == 5 and result["checksum_sha256"] == file["checksum_sha256"]
    metadata = client.get(f"/api/v1/files/{identifier}").json()
    assert not {"path", "object_key", "storage_fingerprint"} & metadata.keys()
    query(
        client,
        lambda session: session.execute(
            delete(FileGrant).where(
                FileGrant.file_id == identifier, FileGrant.action == "content_read"
            )
        ),
    )
    with pytest.raises(ServiceError) as denied:
        client.portal.call(service.local_origin, identifier)
    assert denied.value.code == "file_not_found"  # Even the administrator needs content authority.


def test_local_origin_rechecks_revocation_after_native_io(client, monkeypatch):
    identifier = UUID(upload(client).json()["id"])
    service = file_service(client)
    storage = service.storage.get()
    opened, release = Event(), Event()
    original = storage.open_local

    def pause(*args, **kwargs):
        opened.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(storage, "open_local", pause)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(client.portal.call, service.local_origin, identifier)
        try:
            assert opened.wait(5)
            query(
                client,
                lambda session: session.execute(
                    delete(FileGrant).where(
                        FileGrant.file_id == identifier, FileGrant.action == "content_read"
                    )
                ),
            )
        finally:
            release.set()
        with pytest.raises(ServiceError) as denied:
            future.result(timeout=10)
        assert denied.value.code == "file_not_found"
    with service.storage.lease(identifier):
        pass


def test_local_origin_cancel_waits_for_native_io_before_releasing_lease(client, monkeypatch):
    identifier = UUID(upload(client).json()["id"])
    service = file_service(client)
    entered, stopped = Event(), Event()

    def pause(fingerprint, key, size, checksum, cancelled):
        entered.set()
        assert cancelled.wait(5)
        stopped.set()
        raise ServiceError(503, "storage_unavailable", "isolated cancellation")

    monkeypatch.setattr(service.storage.get(), "open_local", pause)

    async def cancel():
        task = asyncio.create_task(service.local_origin(identifier))
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stopped.is_set()
        with service.storage.lease(identifier):
            pass

    client.portal.call(cancel)


def test_local_origin_rejects_remote_sources_and_replaced_content(client, monkeypatch):
    identifier = UUID(upload(client).json()["id"])
    service = file_service(client)
    with monkeypatch.context() as scope:
        scope.setattr(
            service.storage,
            "get",
            lambda *_: SimpleNamespace(config=SimpleNamespace(driver="s3"), root=None),
        )
        with pytest.raises(ServiceError) as rejected:
            client.portal.call(service.local_origin, identifier)
        assert rejected.value.code == "local_file_required"
    path = Path(client.portal.call(service.local_origin, identifier)["path"])
    path.write_bytes(b"other")
    with pytest.raises(ServiceError) as replaced:
        client.portal.call(service.local_origin, identifier)
    assert replaced.value.status == 503

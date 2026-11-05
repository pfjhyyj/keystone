"""Generic maintenance and local lookup, with real grants, transactions and leases."""

import asyncio
from contextlib import ExitStack
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import func, select
from test_files_dictionary import client as client
from test_files_dictionary import upload
from test_identity import query, token

from keystone.core.errors import ServiceError
from keystone.platform.audit.models import AuditLog
from keystone.platform.files.models import File, FileReference
from keystone.platform.files.services import access as policy
from keystone.platform.files.services.files import FileService
from keystone.platform.files.services.references import FileReferences, FileReferenceService
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
    original = UUID(upload(client, key="original").json()["id"])
    from keystone.platform.files.schemas import Upload

    async def artifact(key):
        return await file_service(client).upload(
            Upload(
                name=key + ".txt",
                expected_size=5,
            ),
            b"hello",
        )

    first, second = [client.portal.call(artifact, key)["id"] for key in ("first", "second")]
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

    limited = bearer(token(client, ["test_files.asset:inspect"])["token"])
    assert client.delete(f"/api/v1/_test/materials/{orphan}", headers=limited).status_code == 403
    assert client.post(f"/api/v1/_test/materials/{orphan}/retire", json={}).status_code == 404


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


def test_local_file_excludes_deletion_and_rejects_remote_or_replaced_content(client, monkeypatch):
    identifier = UUID(upload(client).json()["id"])
    service = file_service(client)

    async def inspect_local():
        async with service.local_file(identifier) as local:
            assert local.size_bytes == 5
            with pytest.raises(ServiceError) as busy:
                with service.storage.lease(identifier):
                    pytest.fail("Protected file acquired an exclusive deletion lock")
            assert busy.value.code == "file_busy"
            return local.path

    with monkeypatch.context() as scope:
        scope.setattr(
            service.storage,
            "get",
            lambda *_: SimpleNamespace(config=SimpleNamespace(driver="s3"), root=None),
        )
        with pytest.raises(ServiceError) as rejected:
            client.portal.call(inspect_local)
        assert rejected.value.code == "local_file_required"
    path = client.portal.call(inspect_local)
    with service.storage.lease(identifier):
        assert path.read_bytes() == b"hello"
    path.write_bytes(b"other")
    with pytest.raises(ServiceError) as replaced:
        client.portal.call(inspect_local)
    assert replaced.value.status == 503

import asyncio
import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from uuid import UUID

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import update
from test_identity import PASSWORD, assign, bearer, create_user, login, query, role, token

from keystone.bootstrap import create_app
from keystone.composition import build_catalog
from keystone.core.asyncio import new_event_loop, run
from keystone.core.config import Settings
from keystone.core.database import Database
from keystone.core.errors import ServiceError
from keystone.core.migrations import migration_config
from keystone.core.time import utcnow
from keystone.platform.audit.services import writer as audit
from keystone.platform.dictionary.services import dictionaries
from keystone.platform.files.leases import initialize, unavailable
from keystone.platform.files.models import File
from keystone.platform.files.services.cleanup import cleanup
from keystone.platform.files.services.grants import FileGrantService
from keystone.platform.files.services.references import FileReferences
from keystone.platform.identity.policy import identity_lock, load_authorization
from keystone.platform.identity.schemas import UserCreate
from keystone.platform.identity.security import Passwords
from keystone.platform.identity.services.initialize import initialize_admin

pytestmark = pytest.mark.postgres
FILE_PERMISSIONS = ["file:create", "file:inspect", "file:content_read", "file:manage"]


def test_manage_receipts_do_not_disclose_metadata(client):
    f = upload(client).json()
    path = f"/api/v1/files/{f['id']}"
    headers = bearer(token(client, ["file:manage"])["token"])
    assert client.get(path, headers=headers).status_code == 403
    changed = client.patch(path, headers=headers, json={"name": "changed", "status": "active"})
    assert changed.status_code == 200 and changed.json() == {"id": f["id"]}
    removed = client.delete(path, headers=headers)
    assert removed.status_code == 202 and removed.json() == {"id": f["id"]}
    assert client.delete(path, headers=headers).json() == {"id": f["id"]}
    assert client.get(path).json()["status"] == "deleting"

    # Functional inspect alone also cannot bypass a missing object grant.
    f = upload(client, key="object-scope").json()
    alice = create_user(client)
    r = role(client, "manager", ["file:inspect", "file:manage"])
    assign(client, alice["id"], r["id"])
    assert (
        add_grant(client, f["id"], "manage", subject_type="user", user_id=alice["id"]).status_code
        == 201
    )
    login(client, "alice")
    client.post(
        "/api/v1/auth/password",
        json={"current_password": PASSWORD, "password": "changed-password-123"},
    )
    login(client, "alice", "changed-password-123")
    path = f"/api/v1/files/{f['id']}"
    assert client.get(path).status_code == 404
    assert client.patch(path, json={"name": "changed", "status": "active"}).json() == {
        "id": f["id"]
    }
    assert client.delete(path).json() == {"id": f["id"]}


def test_dictionary_audits_all_written_defaults(client):
    d = client.post(
        "/api/v1/dictionaries",
        json={
            "key": "review",
            "name": "Review",
            "description": "Original description",
        },
    ).json()
    item = client.post(
        f"/api/v1/dictionaries/{d['id']}/items",
        json={
            "value": "first",
            "label": "First",
            "sort_order": 7,
        },
    ).json()
    cases = [
        (
            f"/api/v1/dictionaries/{d['id']}",
            d["id"],
            {"name": "Edited", "is_active": True},
            "description",
            "Original description",
            None,
        ),
        (
            f"/api/v1/dictionaries/{d['id']}/items/{item['id']}",
            item["id"],
            {"label": "Edited", "is_active": True},
            "sort_order",
            7,
            0,
        ),
    ]
    for path, identifier, data, field, previous, current in cases:
        response = client.patch(path, json=data)
        assert response.status_code == 200 and response.json()[field] == current
        logs = client.get("/api/v1/audit-logs", params={"target_id": identifier}).json()
        log = next(row for row in logs if row["action"].endswith(".update"))
        assert log["details"]["before"][field] == previous
        assert log["details"]["after"][field] == current
        assert log["details"]["before"].keys() == log["details"]["after"].keys()


def test_role_deletion_coordinates_grants_and_audit_rollback(client, monkeypatch):
    f = upload(client).json()
    r = role(client, "deleting", ["file:inspect"])
    entered, release, grant_started = Event(), Event(), Event()
    original_check = FileGrantService.ensure_role_unused

    async def pause(self, session, identifier):
        entered.set()
        await asyncio.to_thread(release.wait, 10)
        await original_check(self, session, identifier)

    def grant():
        grant_started.set()
        return add_grant(client, f["id"], subject_type="role", role_id=r["id"])

    with monkeypatch.context() as patch:
        patch.setattr(FileGrantService, "ensure_role_unused", pause)
        with ThreadPoolExecutor() as pool:
            deletion = pool.submit(client.delete, f"/api/v1/roles/{r['id']}")
            try:
                assert entered.wait(5)
                granting = pool.submit(grant)
                assert grant_started.wait(5)
                with pytest.raises(TimeoutError):
                    granting.result(timeout=0.1)
            finally:
                release.set()
            assert deletion.result().status_code == 204
            assert granting.result().status_code == 404
    assert all(
        g["role_id"] != r["id"] for g in client.get(f"/api/v1/files/{f['id']}/grants").json()
    )

    r = role(client, "rollback_delete", ["file:inspect"])
    original_record = audit.record

    async def fail_success(*args, **kwargs):
        if args[1] == "identity.role.delete" and kwargs.get("outcome", "success") == "success":
            raise RuntimeError("test audit unavailable")
        return await original_record(*args, **kwargs)

    monkeypatch.setattr(audit, "record", fail_success)
    assert client.delete(f"/api/v1/roles/{r['id']}").status_code == 500
    retained = next(row for row in client.get("/api/v1/roles").json() if row["id"] == r["id"])
    assert retained["permissions"] == ["file:inspect"]
    logs = client.get(
        "/api/v1/audit-logs", params={"target_id": r["id"], "action": "identity.role.delete"}
    ).json()
    assert len(logs) == 1 and logs[0]["outcome"] == "failure"


@pytest.fixture
def client(empty_database_url, tmp_path):
    initialize(tmp_path / "storage")
    settings = Settings(
        config_file=None,
        database_url=empty_database_url,
        env="test",
        file_coordination_root=tmp_path / "storage",
        file_storages={"local": {"driver": "filesystem", "root": tmp_path / "storage"}},
        file_max_bytes=1024,
    )
    config = migration_config(settings)
    config.attributes["create_schema"] = True
    command.upgrade(config, "head")

    async def initialize_user():
        database = Database(settings, catalog=build_catalog())
        try:
            await initialize_admin(
                database,
                Passwords(),
                UserCreate(username="admin", display_name="管理员", password=PASSWORD),
            )
        finally:
            await database.close()

    run(initialize_user())
    with TestClient(
        create_app(settings), backend_options={"loop_factory": new_event_loop}
    ) as browser:
        login(browser)
        yield browser


def upload(client, content=b"hello", key="one", **overrides):
    params = {
        "name": "报告.txt",
        "expected_size": len(content),
        "request_key": key,
        "checksum_sha256": hashlib.sha256(content).hexdigest(),
        **overrides,
    }
    return client.post(
        "/api/v1/files",
        params=params,
        content=content,
        headers={"Content-Type": "application/octet-stream"},
    )


def add_grant(client, identifier, action="content_read", **subject):
    return client.post(f"/api/v1/files/{identifier}/grants", json={"action": action, **subject})


def clean(client):
    runtime = client.app.state.runtime
    client.portal.call(cleanup, runtime.database, runtime.storage)


def references(client, user_id, identifier, *, ids=None, slot="attachment", rollback=False):
    async def execute(session):
        await identity_lock(session)
        auth = await load_authorization(session, UUID(user_id))
        await FileReferences(session, auth, "test").replace(
            "record:1", slot, ids if ids is not None else [identifier]
        )
        if rollback:
            raise RuntimeError("business rollback")

    query(client, execute)


def test_upload_idempotency_limits_and_private_metadata(client):
    response = upload(client)
    assert response.status_code == 201, response.text
    file = response.json()
    assert file["status"] == "active" and file["size_bytes"] == 5
    assert not {"object_key", "storage_fingerprint", "upload_key", "upload_digest"} & file.keys()
    assert upload(client).json()["id"] == file["id"]
    assert upload(client, b"other").status_code == 409
    assert upload(client, b"x" * 1025).status_code == 413
    assert upload(client, checksum_sha256="0" * 64).status_code == 422
    assert upload(client, checksum_sha256=None).status_code == 422
    assert upload(client, name="../unsafe").status_code == 422
    assert upload(client, owner_id=file["owner_id"]).status_code == 422
    downloaded = client.get(f"/api/v1/files/{file['id']}/content")
    assert downloaded.content == b"hello"
    assert downloaded.headers["content-disposition"].startswith("attachment;")
    assert downloaded.headers["x-content-type-options"] == "nosniff"
    # Rename does not change the original request digest or overwrite content.
    assert (
        client.patch(
            f"/api/v1/files/{file['id']}", json={"name": "renamed", "status": "active"}
        ).status_code
        == 200
    )
    assert upload(client).json()["name"] == "renamed"
    # A create-only PAT can upload, but receives no metadata or implicit query rights.
    create_only = token(client, ["file:create"])["token"]
    client.headers.update(bearer(create_only))
    try:
        receipt = upload(client, key="create_only")
        assert receipt.status_code == 201 and set(receipt.json()) == {"id"}
        assert client.get(f"/api/v1/files/{receipt.json()['id']}").status_code == 403
    finally:
        del client.headers["Authorization"]


def test_object_grants_owner_admin_exception_and_pat_scope(client):
    admin = client.get("/api/v1/auth/me").json()["user"]
    alice = create_user(client)
    r = role(client, "files", FILE_PERMISSIONS)
    assert assign(client, alice["id"], r["id"]).status_code == 204
    # Test helper clears initial-password requirement through the real password flow.
    login(client, "alice")
    changed = client.post(
        "/api/v1/auth/password",
        json={"current_password": PASSWORD, "password": "changed-password-123"},
    )
    assert changed.status_code == 204, changed.text
    login(client, "alice", "changed-password-123")
    f = upload(client).json()
    read_pat = token(client, ["file:content_read"])["token"]
    grants = client.get(f"/api/v1/files/{f['id']}/grants").json()
    for g in sorted(grants, key=lambda g: g["action"] == "manage"):
        assert client.delete(f"/api/v1/files/{f['id']}/grants/{g['id']}").status_code == 204
    assert client.get(f"/api/v1/files/{f['id']}").status_code == 404
    assert client.get("/api/v1/files").json() == []
    assert upload(client).status_code == 404  # Owner cannot restore removed grants by retry.
    login(client)
    assert client.get(f"/api/v1/files/{f['id']}").status_code == 200
    assert client.get(f"/api/v1/files/{f['id']}/content").status_code == 404
    assert add_grant(client, f["id"], subject_type="user", user_id=admin["id"]).status_code == 201
    assert client.get(f"/api/v1/files/{f['id']}/content").content == b"hello"
    assert add_grant(client, f["id"], subject_type="user", user_id=alice["id"]).status_code == 201
    assert (
        client.get(f"/api/v1/files/{f['id']}/content", headers=bearer(read_pat)).status_code == 200
    )
    assert client.get(f"/api/v1/files/{f['id']}", headers=bearer(read_pat)).status_code == 403
    assert (
        add_grant(client, f["id"], action="manage", subject_type="authenticated").status_code == 422
    )


def test_references_transactions_disable_and_delete_cleanup(client, monkeypatch):
    me = client.get("/api/v1/auth/me").json()["user"]["id"]
    f = upload(client).json()
    with pytest.raises(RuntimeError):
        references(client, me, f["id"], rollback=True)
    assert client.get(f"/api/v1/files/{f['id']}/references").json() == []
    references(client, me, f["id"])
    assert client.delete(f"/api/v1/files/{f['id']}").json()["code"] == "file_referenced"
    client.patch(f"/api/v1/files/{f['id']}", json={"name": "off", "status": "disabled"})
    references(client, me, f["id"])  # Retaining old relation never requires re-read.
    with pytest.raises(ServiceError, match="文件当前不可读取"):
        references(client, me, f["id"], slot="new")
    references(client, me, f["id"], ids=[])
    assert client.delete(f"/api/v1/files/{f['id']}").status_code == 202
    storage = client.app.state.runtime.storage.get()
    original = storage.delete
    monkeypatch.setattr(storage, "delete", lambda *_: (_ for _ in ()).throw(unavailable()))
    clean(client)
    assert client.get(f"/api/v1/files/{f['id']}").json()["deleted_at"] is None
    assert len(client.get(f"/api/v1/files/{f['id']}/grants").json()) == 3
    assert add_grant(client, f["id"], subject_type="authenticated").status_code == 409
    monkeypatch.setattr(storage, "delete", original)
    clean(client)
    row = client.get(f"/api/v1/files/{f['id']}").json()
    assert row["status"] == "deleted" and row["deleted_at"]
    assert client.get(f"/api/v1/files/{f['id']}/grants").json() == []
    assert upload(client).status_code == 409
    logs = client.get("/api/v1/audit-logs", params={"target_id": f["id"]})
    assert logs.status_code == 200


def test_role_grant_restricts_deletion_until_file_cleanup(client):
    f = upload(client).json()
    r = role(client, "reader", ["file:content_read"])
    grant = add_grant(client, f["id"], subject_type="role", role_id=r["id"])
    assert grant.status_code == 201
    assert (
        add_grant(client, f["id"], subject_type="role", role_id=r["id"]).json()["id"]
        == grant.json()["id"]
    )
    assert client.delete(f"/api/v1/roles/{r['id']}").status_code == 409
    client.delete(f"/api/v1/files/{f['id']}")
    assert client.delete(f"/api/v1/roles/{r['id']}").status_code == 409
    clean(client)
    assert client.delete(f"/api/v1/roles/{r['id']}").status_code == 204


def test_failed_upload_retry_preserves_row_and_revoked_grants(client, monkeypatch):
    storage = client.app.state.runtime.storage.get()
    original = storage.write

    def fail_after_write(*args):
        original(*args)
        raise unavailable()

    monkeypatch.setattr(storage, "write", fail_after_write)
    assert upload(client).status_code == 503
    f = client.get("/api/v1/files").json()[0]
    assert f["status"] == "failed"
    grants = client.get(f"/api/v1/files/{f['id']}/grants").json()
    read_grant = next(g for g in grants if g["action"] == "content_read")
    client.delete(f"/api/v1/files/{f['id']}/grants/{read_grant['id']}")
    monkeypatch.setattr(storage, "write", original)
    retried = upload(client)
    assert retried.status_code == 201 and retried.json()["id"] == f["id"]
    assert client.get(f"/api/v1/files/{f['id']}/content").status_code == 404
    assert len(client.get(f"/api/v1/files/{f['id']}/grants").json()) == 2


def test_upload_delete_and_revocation_while_storage_is_writing(client, monkeypatch):
    storage = client.app.state.runtime.storage.get()
    entered, release = Event(), Event()
    original = storage.write

    def pause(*args):
        entered.set()
        assert release.wait(10)
        original(*args)

    monkeypatch.setattr(storage, "write", pause)
    with ThreadPoolExecutor() as pool:
        pending = pool.submit(upload, client)
        try:
            assert entered.wait(5)
            f = client.get("/api/v1/files").json()[0]
            assert upload(client).json()["code"] == "file_busy"
            assert client.delete(f"/api/v1/files/{f['id']}").status_code == 202
            clean(client)  # Live writer holds filesystem lease: no premature deletion.
            assert client.get(f"/api/v1/files/{f['id']}").json()["status"] == "deleting"
        finally:
            release.set()
        assert pending.result().json()["status"] == "deleting"
    clean(client)
    assert client.get(f"/api/v1/files/{f['id']}").json()["status"] == "deleted"
    assert not (storage.root / "files" / UUID(f["id"]).hex).exists()


def test_volume_identity_and_abandoned_upload_recovery(client):
    f = upload(client).json()

    async def abandon(session):
        await session.execute(
            update(File)
            .where(File.id == UUID(f["id"]))
            .values(status="uploading", updated_at=utcnow() - timedelta(hours=2))
        )

    query(client, abandon)
    clean(client)
    assert client.get(f"/api/v1/files/{f['id']}").json()["status"] == "failed"
    assert upload(client).json()["id"] == f["id"]
    marker = client.app.state.runtime.storage.get().root / ".keystone-storage"
    marker.write_text("keystone:" + "1" * 64)
    assert client.get(f"/api/v1/files/{f['id']}/content").status_code == 503
    assert client.get("/api/v1/ready").status_code == 503


def test_dictionary_stable_keys_history_and_audit_rollback(client, monkeypatch):
    d = client.post("/api/v1/dictionaries", json={"key": "source", "name": "来源"}).json()
    assert (
        client.post("/api/v1/dictionaries", json={"key": "source", "name": "重复"}).status_code
        == 409
    )
    path = f"/api/v1/dictionaries/{d['id']}"
    item = client.post(path + "/items", json={"value": "referral", "label": "推荐"}).json()
    assert (
        client.post(path + "/items", json={"value": "referral", "label": "重复"}).status_code == 409
    )
    assert (
        client.patch(path, json={"key": "changed", "name": "X", "is_active": True}).status_code
        == 422
    )

    def edit_parent(active):
        return client.patch(path, json={"name": "来源", "is_active": active})

    edit_parent(False)
    assert client.get("/api/v1/dictionaries/by-key/source/options").json() == []

    async def validate(session, previous=None):
        return await dictionaries.validate_selection(
            session, "source", UUID(item["id"]), previous_id=previous
        )

    with pytest.raises(ServiceError):
        query(client, validate)
    assert query(client, lambda s: validate(s, UUID(item["id"]))) == UUID(item["id"])
    assert (
        client.get(f"/api/v1/dictionaries/by-key/source/items/{item['id']}").json()["label"]
        == "推荐"
    )
    assert client.get(f"/api/v1/dictionaries/by-key/wrong/items/{item['id']}").status_code == 422
    client.patch(path + f"/items/{item['id']}", json={"label": "停用项", "is_active": False})
    edit_parent(True)
    assert client.get("/api/v1/dictionaries/by-key/source/options").json() == []
    assert client.delete(path).status_code == 405
    from keystone.platform.audit.services import writer as audit

    original = audit.record

    async def fail(session, action, *args, **kwargs):
        if (
            action == "dictionary.definition.update"
            and kwargs.get("outcome", "success") == "success"
        ):
            raise RuntimeError("audit unavailable")
        await original(session, action, *args, **kwargs)

    monkeypatch.setattr(audit, "record", fail)
    assert edit_parent(False).status_code == 500
    assert client.get("/api/v1/dictionaries").json()[0]["is_active"] is True


def test_dictionary_business_validation_serializes_with_disable(client):
    d = client.post("/api/v1/dictionaries", json={"key": "source", "name": "来源"}).json()
    item = client.post(
        f"/api/v1/dictionaries/{d['id']}/items", json={"value": "referral", "label": "推荐"}
    ).json()

    async def competition():
        db = client.app.state.runtime.database
        selected, finish = asyncio.Event(), asyncio.Event()

        async def business():
            async with db.transaction() as session:
                await dictionaries.validate_selection(session, "source", UUID(item["id"]))
                selected.set()
                await finish.wait()

        async def disable():
            await selected.wait()
            async with db.transaction() as session:
                row = await dictionaries.locked_dictionary(session, key="source")
                row.is_active = False

        a, b = asyncio.create_task(business()), asyncio.create_task(disable())
        await selected.wait()
        await asyncio.sleep(0.05)
        assert not b.done()
        finish.set()
        await asyncio.gather(a, b)
        async with db.transaction() as session:
            with pytest.raises(ServiceError):
                await dictionaries.validate_selection(session, "source", UUID(item["id"]))

    client.portal.call(competition)


def test_generic_dictionary_requires_permission_but_scoped_business_lookup_does_not(client):
    d = client.post("/api/v1/dictionaries", json={"key": "source", "name": "来源"}).json()
    item = client.post(
        f"/api/v1/dictionaries/{d['id']}/items", json={"value": "referral", "label": "推荐"}
    ).json()
    pat = token(client, ["file:inspect"])["token"]
    assert client.get("/api/v1/dictionaries", headers=bearer(pat)).status_code == 403
    assert (
        client.get("/api/v1/dictionaries/by-key/source/options", headers=bearer(pat)).status_code
        == 403
    )
    # Trusted business service uses fixed key, not a public permission-bypass endpoint.
    assert (
        query(client, lambda s: dictionaries.resolve(s, "source", UUID(item["id"])))["label"]
        == "推荐"
    )


def test_reference_wins_or_deletion_wins_without_partial_business_commit(client):
    user_id = UUID(client.get("/api/v1/auth/me").json()["user"]["id"])
    f = upload(client).json()
    file_id = UUID(f["id"])

    async def competition():
        db = client.app.state.runtime.database
        bound, finish = asyncio.Event(), asyncio.Event()

        async def bind():
            async with db.transaction() as session:
                await identity_lock(session)
                auth = await load_authorization(session, user_id)
                await FileReferences(session, auth, "test").replace("record:1", "input", [file_id])
                bound.set()
                await finish.wait()

        async def deletion():
            from keystone.platform.files.policy import locked
            from keystone.platform.files.services.references import require_unreferenced

            await bound.wait()
            async with db.transaction() as session:
                file = await locked(session, file_id)
                await require_unreferenced(session, file_id)
                file.status = "deleting"

        a, b = asyncio.create_task(bind()), asyncio.create_task(deletion())
        await bound.wait()
        await asyncio.sleep(0.05)
        assert not b.done()
        finish.set()
        await a
        with pytest.raises(ServiceError) as rejected:
            await b
        assert rejected.value.code == "file_referenced"

    client.portal.call(competition)
    references(client, str(user_id), file_id, ids=[], slot="input")
    client.delete(f"/api/v1/files/{file_id}")
    with pytest.raises(ServiceError):
        references(client, str(user_id), file_id)
    assert client.get(f"/api/v1/files/{file_id}/references").json() == []


def test_completion_does_not_restore_grants_after_revocation(client, monkeypatch):
    alice = create_user(client)
    r = role(client, "files", FILE_PERMISSIONS)
    assign(client, alice["id"], r["id"])
    login(client, "alice")
    client.post(
        "/api/v1/auth/password",
        json={"current_password": PASSWORD, "password": "changed-password-123"},
    )
    login(client, "alice", "changed-password-123")
    storage = client.app.state.runtime.storage.get()
    original = storage.write
    entered, release = Event(), Event()

    def pause(*args):
        entered.set()
        assert release.wait(10)
        original(*args)

    monkeypatch.setattr(storage, "write", pause)
    with ThreadPoolExecutor() as pool:
        future = pool.submit(upload, client)
        try:
            assert entered.wait(5)
            f = client.get("/api/v1/files").json()[0]
            # Revoke all initial grants while the already-authorized write is completing.
            grants = client.get(f"/api/v1/files/{f['id']}/grants").json()
            for g in sorted(grants, key=lambda g: g["action"] == "manage"):
                assert client.delete(f"/api/v1/files/{f['id']}/grants/{g['id']}").status_code == 204
        finally:
            release.set()
        assert future.result().status_code == 404
    login(client)
    assert client.get(f"/api/v1/files/{f['id']}").json()["status"] == "active"
    assert client.get(f"/api/v1/files/{f['id']}/grants").json() == []


def test_delete_completion_audit_failure_rolls_back_metadata_and_grant_removal(client, monkeypatch):
    from keystone.platform.audit.services import writer as audit

    f = upload(client).json()
    client.delete(f"/api/v1/files/{f['id']}")
    original = audit.record

    async def fail(session, action, *args, **kwargs):
        if action == "file.delete.complete":
            raise RuntimeError("audit unavailable")
        await original(session, action, *args, **kwargs)

    monkeypatch.setattr(audit, "record", fail)
    with pytest.raises(RuntimeError):
        clean(client)
    assert client.get(f"/api/v1/files/{f['id']}").json()["status"] == "deleting"
    assert len(client.get(f"/api/v1/files/{f['id']}/grants").json()) == 3
    monkeypatch.setattr(audit, "record", original)
    clean(client)  # OpenDAL delete is idempotent when bytes were already removed.
    assert client.get(f"/api/v1/files/{f['id']}").json()["status"] == "deleted"


def test_local_lease_is_cross_process_and_native_io_finishes_before_cancellation(tmp_path):
    import subprocess
    import sys

    from keystone.platform.files.leases import LocalLeases
    from keystone.platform.files.storage import blocking

    initialize(tmp_path / "store")
    storage = LocalLeases(tmp_path / "store")
    with storage.lease("one"):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from keystone.platform.files.leases import LocalLeases; import sys; "
                'LocalLeases(sys.argv[1]).lease("one").__enter__()',
                str(storage.root),
            ],
            capture_output=True,
            timeout=10,
        )
        assert result.returncode != 0 and b"file" in result.stderr.lower()
    with storage.lease("one"):
        pass

    async def cancel_io():
        started, finish = Event(), Event()

        def native():
            started.set()
            assert finish.wait(5)

        task = asyncio.create_task(blocking(native))
        while not started.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(cancel_io())

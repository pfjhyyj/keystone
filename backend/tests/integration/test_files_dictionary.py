import asyncio
import hashlib
from datetime import timedelta
from uuid import UUID

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text, update
from support.business_api import create_app
from support.business_catalog import install_catalog
from test_identity import PASSWORD, bearer, login, query, token

from keystone.composition import build_catalog
from keystone.core.asyncio import new_event_loop, run
from keystone.core.config import Settings
from keystone.core.database import Database
from keystone.core.errors import ServiceError
from keystone.core.migrations import migration_config
from keystone.core.time import utcnow
from keystone.platform.audit.services import writer as audit
from keystone.platform.dictionary.services import dictionaries
from keystone.platform.files.adapters.storage import initialize
from keystone.platform.files.models import File
from keystone.platform.files.services.cleanup import cleanup
from keystone.platform.files.services.references import FileReferences
from keystone.platform.identity.adapters.security import Passwords
from keystone.platform.identity.schemas import UserCreate
from keystone.platform.identity.services.authorization import identity_lock, load_authorization
from keystone.platform.identity.services.initialize import initialize_admin

pytestmark = pytest.mark.postgres
FILE_PERMISSIONS = [
    "test_files.asset:create",
    "test_files.asset:inspect",
    "test_files.asset:content_read",
    "test_files.asset:manage",
]


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


@pytest.fixture
def client(empty_database_url, tmp_path, monkeypatch):
    install_catalog(monkeypatch)
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
        "/api/v1/_test/materials",
        params=params,
        content=content,
        headers={"Content-Type": "application/octet-stream"},
    )


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
    downloaded = client.get(f"/api/v1/_test/materials/{file['id']}/content")
    assert downloaded.content == b"hello"
    assert downloaded.headers["content-disposition"].startswith("attachment;")
    assert downloaded.headers["x-content-type-options"] == "nosniff"
    # Rename does not change the original request digest or overwrite content.
    assert (
        client.patch(
            f"/api/v1/_test/materials/{file['id']}", json={"name": "renamed", "status": "active"}
        ).status_code
        == 200
    )
    assert upload(client).json()["name"] == "renamed"
    # A create-only PAT can upload, but receives no metadata or implicit query rights.
    create_only = token(client, ["test_files.asset:create"])["token"]
    client.headers.update(bearer(create_only))
    try:
        receipt = upload(client, key="create_only")
        assert receipt.status_code == 201 and set(receipt.json()) == {"id"}
        assert client.get(f"/api/v1/_test/materials/{receipt.json()['id']}").status_code == 403
    finally:
        del client.headers["Authorization"]


def test_volume_identity_and_abandoned_upload_recovery(client):
    f = upload(client).json()

    async def abandon(session):
        await session.execute(
            text("DELETE FROM keystone.file_references WHERE file_id=:id"), {"id": UUID(f["id"])}
        )
        await session.execute(
            update(File)
            .where(File.id == UUID(f["id"]))
            .values(status="uploading", updated_at=utcnow() - timedelta(hours=2))
        )

    query(client, abandon)
    clean(client)
    assert client.get(f"/api/v1/_test/materials/{f['id']}").json()["status"] == "failed"
    assert upload(client).json()["id"] == f["id"]
    marker = client.app.state.runtime.storage.get().root / ".keystone-storage"
    marker.write_text("keystone:" + "1" * 64)
    assert client.get(f"/api/v1/_test/materials/{f['id']}/content").status_code == 503
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
    pat = token(client, ["test_files.asset:inspect"])["token"]
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


def test_delete_completion_audit_failure_rolls_back_metadata_and_grant_removal(client, monkeypatch):

    f = upload(client).json()
    client.delete(f"/api/v1/_test/materials/{f['id']}")
    original = audit.record

    async def fail(session, action, *args, **kwargs):
        if action == "file.delete.complete":
            raise RuntimeError("audit unavailable")
        await original(session, action, *args, **kwargs)

    monkeypatch.setattr(audit, "record", fail)
    with pytest.raises(RuntimeError):
        clean(client)
    assert client.get(f"/api/v1/_test/materials/{f['id']}").json()["status"] == "deleting"
    monkeypatch.setattr(audit, "record", original)
    clean(client)  # OpenDAL delete is idempotent when bytes were already removed.
    assert client.get(f"/api/v1/_test/materials/{f['id']}").json()["status"] == "deleted"


def file_count(client):
    return query(client, lambda session: session.scalar(select(func.count()).select_from(File)))


def reference_rows(client, identifier):
    from keystone.platform.files.models import FileReference

    return (
        query(
            client,
            lambda session: session.execute(
                select(FileReference.owner, FileReference.slot)
                .where(FileReference.file_id == UUID(str(identifier)))
                .order_by(FileReference.owner, FileReference.slot)
            ),
        )
        .mappings()
        .all()
    )


def stored_files(client):
    from fastapi.encoders import jsonable_encoder

    from keystone.platform.files.schemas import view

    return [
        jsonable_encoder(view(row))
        for row in query(client, lambda session: session.scalars(select(File))).all()
    ]


def stored_file(client, identifier):
    return next(row for row in stored_files(client) if row["id"] == str(identifier))


def stored_content(client, identifier):
    """Trusted fixture verification; not a product download API."""
    from keystone.platform.identity.schemas.credentials import Credential
    from keystone.platform.identity.services.access import AccessContext

    runtime = client.app.state.runtime
    identity = AccessContext(
        runtime.database,
        runtime.settings,
        Credential("session", client.cookies["keystone_session"]),
    )
    return client.portal.call(runtime.managed_files(identity).content, UUID(str(identifier)))[1]

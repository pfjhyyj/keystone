"""Business-authorized attachments and opt-in reclamation on real PostgreSQL/storage."""

import asyncio
import hashlib
from contextlib import asynccontextmanager
from datetime import timedelta

import pytest
from dbos import DBOS
from fastapi import Request
from sqlalchemy import func, select, text, update
from test_business_integration import ACTION, READ, WRITE, install
from test_files_dictionary import client as client
from test_flow import worker
from test_identity import (
    NEW_PASSWORD,
    PASSWORD,
    assign,
    create_user,
    login,
    query,
    role,
)
from test_maintenance import wait_for

from keystone.core.content_response import ContentResponse
from keystone.core.errors import ServiceError
from keystone.core.time import utcnow
from keystone.platform.audit.models import AuditLog
from keystone.platform.audit.services import writer as audit
from keystone.platform.files.models import File, FileReference
from keystone.platform.files.schemas import Upload
from keystone.platform.files.services import access as file_access
from keystone.platform.files.services.cleanup import backlog, cleanup
from keystone.platform.files.services.references import FileReferences
from keystone.platform.identity.controllers.dependencies import access_context
from keystone.platform.identity.schemas.credentials import Credential
from keystone.platform.identity.services.access import AccessContext
from keystone.platform.identity.services.authorization import identity_lock

pytestmark = pytest.mark.postgres


class Attachments:
    """Only the test business knows its object ownership; Files receives exact IDs."""

    def __init__(self, identity, files):
        self.identity, self.files = identity, files

    async def _record(self, session, auth, key, *, creating=False):
        row = (
            (
                await session.execute(
                    text("SELECT * FROM keystone.test_attachments WHERE key=:key FOR UPDATE"),
                    {"key": key},
                )
            )
            .mappings()
            .first()
        )
        if (row is None and not creating) or (row and row["owner_id"] != auth.user.id):
            raise ServiceError(404, "note_not_found", "业务对象不可访问")
        return row

    async def save(self, key, content, *, operation="save", after_upload=None):
        async with self.identity.access(WRITE) as (session, auth):
            await self._record(session, auth, key, creating=True)
        uploaded = await self.files.upload(
            Upload(
                name="attachment.txt",
                content_type="text/plain",
                expected_size=len(content),
                checksum_sha256=hashlib.sha256(content).hexdigest(),
                request_key=f"note:{key}:{operation}",
            ),
            content,
        )
        if after_upload is not None:
            await after_upload()
        async with self.identity.access(
            WRITE, action=ACTION, target="test_notes.note", target_id=key
        ) as (session, auth):
            await self._record(session, auth, key, creating=True)
            await session.execute(
                text("""INSERT INTO keystone.test_attachments (key,owner_id,file_id)
                    VALUES (:key,:owner,:file) ON CONFLICT (key)
                    DO UPDATE SET file_id=EXCLUDED.file_id"""),
                {"key": key, "owner": auth.user.id, "file": uploaded["id"]},
            )
            await FileReferences(session, auth, "test_notes").replace(
                key, "attachment", [uploaded["id"]]
            )
            await self.identity.log(
                session, auth, ACTION, "test_notes.note", key, {"status": "saved"}
            )
        return uploaded

    @asynccontextmanager
    async def content(self, key):
        # Reserve capacity before business authorization if the request must queue.
        async with self.files.read_transfer() as transfer:
            async with self.identity.access(READ) as (session, auth):
                row = await self._record(session, auth, key)
                identifier = row["file_id"]
            async with transfer.open_content(identifier) as reader:
                yield reader

    async def remove(self, key):
        async with self.identity.access(
            WRITE, action=ACTION, target="test_notes.note", target_id=key
        ) as (session, auth):
            await self._record(session, auth, key)
            await FileReferences(session, auth, "test_notes").release(key, "attachment")
            await session.execute(
                text("DELETE FROM keystone.test_attachments WHERE key=:key"), {"key": key}
            )
            await self.identity.log(
                session, auth, ACTION, "test_notes.note", key, {"status": "removed"}
            )


def business(browser):
    runtime = browser.app.state.runtime
    identity = AccessContext(
        runtime.database,
        runtime.settings,
        Credential("session", browser.cookies["keystone_session"]),
    )
    return Attachments(identity, runtime.managed_files(identity))


@pytest.fixture
def notes(monkeypatch, request):
    install(monkeypatch)
    browser = request.getfixturevalue("client")
    query(
        browser,
        lambda session: session.execute(
            text("""CREATE TABLE keystone.test_attachments (
                key text PRIMARY KEY, owner_id uuid NOT NULL, file_id uuid NOT NULL)""")
        ),
    )
    user = create_user(browser)
    assigned = role(browser, "attachments", [READ, WRITE])
    assert assign(browser, user["id"], assigned["id"]).status_code == 204
    login(browser, "alice")
    browser.post(
        "/api/v1/auth/password", json={"current_password": PASSWORD, "password": NEW_PASSWORD}
    )
    login(browser, "alice", NEW_PASSWORD)

    @browser.app.api_route("/_notes/{key}/content", methods=["GET", "HEAD"])
    async def content(key: str, request: Request):
        runtime = request.app.state.runtime
        identity = access_context(request)
        service = Attachments(identity, runtime.managed_files(identity))
        return ContentResponse(service.content(key), request)

    return browser


def references(browser, identifier):
    return query(
        browser,
        lambda session: session.scalar(
            select(func.count())
            .select_from(FileReference)
            .where(FileReference.file_id == identifier)
        ),
    )


def test_business_attachment_save_read_replace_and_remove_without_file_grants(notes):
    service = business(notes)
    first = notes.portal.call(service.save, "one", b"first")
    repeated = notes.portal.call(service.save, "one", b"first")
    assert repeated["id"] == first["id"]
    assert references(notes, first["id"]) == 1
    assert (
        query(notes, lambda s: s.scalar(text("SELECT to_regclass('keystone.file_grants')"))) is None
    )
    assert notes.get("/_notes/one/content").content == b"first"
    response = notes.get("/_notes/one/content", headers={"Range": "bytes=1-3"})
    assert response.status_code == 206 and response.content == b"irs"
    assert notes.head("/_notes/one/content").headers["content-length"] == "5"
    assert notes.get(f"/api/v1/files/{first['id']}/content").status_code == 404
    assert not {"path", "object_key", "storage_fingerprint"} & first.keys()
    second = notes.portal.call(lambda: service.save("one", b"second", operation="replace"))
    assert second["id"] != first["id"]
    assert references(notes, first["id"]) == 0
    assert references(notes, second["id"]) == 1
    assert notes.get("/_notes/one/content").content == b"second"
    notes.portal.call(service.remove, "one")
    assert references(notes, second["id"]) == 0
    assert (
        query(notes, lambda s: s.scalar(select(File.status).where(File.id == second["id"])))
        == "active"
    )


def test_business_authorizes_objects_and_current_permissions(notes):
    notes.portal.call(business(notes).save, "one", b"private")
    login(notes)
    # Even the administrator cannot read a business object without its object scope.
    assert notes.get("/_notes/one/content").status_code == 404
    login(notes, "alice", NEW_PASSWORD)
    assert notes.get("/_notes/one/content").status_code == 200
    login(notes)
    assigned = next(row for row in notes.get("/api/v1/roles").json() if row["key"] == "attachments")
    assert (
        notes.put(
            f"/api/v1/roles/{assigned['id']}", json={"name": "attachments", "permissions": [WRITE]}
        ).status_code
        == 200
    )
    login(notes, "alice", NEW_PASSWORD)
    assert notes.get("/_notes/one/content").status_code == 403


def test_business_audit_failure_rolls_back_relation_and_reference_but_keeps_file(
    notes, monkeypatch
):
    record = audit.record

    async def fail_business(*args, **kwargs):
        if args[1] == ACTION and kwargs.get("outcome", "success") == "success":
            raise RuntimeError("test business audit failure")
        return await record(*args, **kwargs)

    monkeypatch.setattr(audit, "record", fail_business)
    with pytest.raises(RuntimeError, match="business audit"):
        notes.portal.call(business(notes).save, "one", b"kept")
    assert (
        query(notes, lambda s: s.scalar(text("SELECT count(*) FROM keystone.test_attachments")))
        == 0
    )
    identifier = query(notes, lambda s: s.scalar(select(File.id)))
    assert references(notes, identifier) == 0
    assert query(notes, lambda s: s.scalar(select(File.status))) == "active"
    assert (
        query(
            notes,
            lambda s: s.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "file.reference")
            ),
        )
        == 0
    )
    monkeypatch.setattr(audit, "record", record)
    assert notes.portal.call(business(notes).save, "one", b"kept")["id"] == identifier


def test_business_rechecks_save_permission_after_upload(notes):
    service = business(notes)

    async def revoke():
        async with notes.app.state.runtime.database.transaction() as session:
            await identity_lock(session, identity_write=True)
            await session.execute(
                text("DELETE FROM keystone.role_permissions WHERE permission_key=:permission"),
                {"permission": WRITE},
            )

    with pytest.raises(ServiceError) as denied:
        notes.portal.call(lambda: service.save("one", b"kept", after_upload=revoke))
    assert denied.value.code == "permission_denied"
    assert (
        query(notes, lambda s: s.scalar(text("SELECT count(*) FROM keystone.test_attachments")))
        == 0
    )
    assert query(notes, lambda s: s.scalar(select(File.status))) == "active"


def test_managed_local_transfer_can_save_without_premature_business_references(notes):
    service = business(notes)

    async def save():
        async with service.identity.access(WRITE):
            pass
        with service.files.transfer() as transfer:
            async with transfer.workspace(4) as path:
                path.write_bytes(b"data")
                with service.files.transfer() as writer:
                    stored = await writer.upload_file(
                        path,
                        name="generated.bin",
                        content_type="application/octet-stream",
                        request_key="generated:one",
                    )
        with service.files.transfer() as transfer:
            async with transfer.materialize_file(stored["id"]) as path:
                assert path.read_bytes() == b"data"
        return stored["id"]

    identifier = notes.portal.call(save)
    assert references(notes, identifier) == 0
    assert (
        query(notes, lambda s: s.scalar(text("SELECT to_regclass('keystone.file_grants')"))) is None
    )


@pytest.mark.parametrize("retention", [None, 3600])
def test_reclamation_requires_opt_in_completion_grace_and_no_references(notes, retention):
    service = business(notes)
    rows = [notes.portal.call(service.save, str(i), b"data") for i in range(5)]
    for i in range(1, 5):
        notes.portal.call(service.remove, str(i))
    old = utcnow() - timedelta(hours=2)
    query(notes, lambda s: s.execute(update(File).values(completed_at=old)))
    query(
        notes,
        lambda s: s.execute(
            update(File).where(File.id == rows[2]["id"]).values(completed_at=utcnow())
        ),
    )
    query(
        notes,
        lambda s: s.execute(update(File).where(File.id == rows[3]["id"]).values(completed_at=None)),
    )
    query(
        notes,
        lambda s: s.execute(update(File).where(File.id == rows[4]["id"]).values(status="disabled")),
    )
    rt = notes.app.state.runtime
    pending, _ = notes.portal.call(
        lambda: backlog(rt.database, unreferenced_grace_seconds=retention)
    )
    assert pending == (1 if retention else 0)
    with rt.storage.lease(rows[1]["id"]):
        notes.portal.call(
            lambda: cleanup(rt.database, rt.storage, unreferenced_grace_seconds=retention)
        )
        assert (
            query(notes, lambda s: s.scalar(select(File.status).where(File.id == rows[1]["id"])))
            == "active"
        )
    notes.portal.call(
        lambda: cleanup(rt.database, rt.storage, unreferenced_grace_seconds=retention)
    )
    statuses = query(notes, lambda s: s.execute(select(File.id, File.status)))
    statuses = dict(statuses.all())
    assert [statuses[row["id"]] for row in rows] == [
        "active",
        "deleted" if retention else "active",
        "active",
        "active",
        "disabled",
    ]
    # Reclamation is based on completion, never on a mutable updated_at/access time.
    if retention:
        assert references(notes, rows[0]["id"]) == 1


def test_reference_and_reclamation_acceptance_are_transactionally_exclusive(notes, monkeypatch):
    service = business(notes)
    saved = notes.portal.call(service.save, "one", b"kept")
    notes.portal.call(service.remove, "one")
    query(
        notes, lambda s: s.execute(update(File).values(completed_at=utcnow() - timedelta(hours=2)))
    )
    rt = notes.app.state.runtime

    async def competition():
        bound, release, cleaning = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original = file_access.locked
        cleaner = None

        async def observed_lock(session, identifier):
            if asyncio.current_task() is cleaner:
                cleaning.set()
            return await original(session, identifier)

        monkeypatch.setattr(file_access, "locked", observed_lock)

        async def reference():
            async with service.identity.access(WRITE) as (session, auth):
                await FileReferences(session, auth, "test_notes").replace(
                    "retained", "attachment", [saved["id"]]
                )
                bound.set()
                await release.wait()

        writer = asyncio.create_task(reference())
        await bound.wait()
        cleaner = asyncio.create_task(
            cleanup(rt.database, rt.storage, unreferenced_grace_seconds=1)
        )
        await asyncio.wait_for(cleaning.wait(), 5)
        assert not cleaner.done()
        release.set()
        await asyncio.gather(writer, cleaner)

    notes.portal.call(competition)
    assert query(notes, lambda s: s.scalar(select(File.status))) == "active"


def test_reclamation_marks_deleting_before_io_and_rejects_new_references(notes, monkeypatch):
    service = business(notes)
    saved = notes.portal.call(service.save, "one", b"kept")
    notes.portal.call(service.remove, "one")
    query(
        notes, lambda s: s.execute(update(File).values(completed_at=utcnow() - timedelta(hours=2)))
    )
    rt = notes.app.state.runtime
    source = rt.storage.get()
    original = source.delete

    def fail_delete(*args):
        raise ServiceError(503, "storage_unavailable", "test storage failure")

    monkeypatch.setattr(source, "delete", fail_delete)
    notes.portal.call(lambda: cleanup(rt.database, rt.storage, unreferenced_grace_seconds=1))
    assert query(notes, lambda s: s.scalar(select(File.status))) == "deleting"

    async def reference():
        async with service.identity.access(WRITE) as (session, auth):
            await FileReferences(session, auth, "test_notes").replace(
                "one", "attachment", [saved["id"]]
            )

    with pytest.raises(ServiceError) as rejected:
        notes.portal.call(reference)
    assert rejected.value.code == "file_unavailable"
    assert references(notes, saved["id"]) == 0
    monkeypatch.setattr(source, "delete", original)
    notes.portal.call(lambda: cleanup(rt.database, rt.storage, unreferenced_grace_seconds=1))
    assert query(notes, lambda s: s.scalar(select(File.status))) == "deleted"


def test_worker_applies_explicit_reclamation_configuration(notes, tmp_path):
    service = business(notes)
    saved = notes.portal.call(service.save, "one", b"kept")
    notes.portal.call(service.remove, "one")
    query(
        notes,
        lambda s: s.execute(update(File).values(completed_at=utcnow() - timedelta(hours=2))),
    )
    rt = notes.app.state.runtime
    rt.settings.file_unreferenced_grace_seconds = 1
    DBOS.migrate(rt.settings.database_url.get_secret_value())
    with worker(notes, tmp_path, stub=False) as (process, log):
        wait_for(
            notes,
            process,
            log,
            lambda: query(notes, lambda s: s.scalar(select(File.status))) == "deleted",
        )
    assert references(notes, saved["id"]) == 0

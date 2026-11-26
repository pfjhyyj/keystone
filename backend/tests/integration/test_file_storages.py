import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from uuid import UUID

import pytest
from sqlalchemy import func, select, text, update
from support.storage_support import cloud_config
from support.storage_support import s3 as s3
from test_files_dictionary import clean, make_file_settings, open_client, stored_files, upload
from test_files_dictionary import client as client
from test_identity import bearer, initialize_database, query, token

from keystone.core.advisory import lock_key
from keystone.core.errors import ServiceError
from keystone.core.time import utcnow
from keystone.platform.files.adapters.storage import Storage, StorageRegistry
from keystone.platform.files.models import File
from keystone.platform.files.services.cleanup import cleanup
from keystone.platform.files.services.files import FileService
from keystone.platform.files.services.references import FileReferenceService
from keystone.platform.identity.schemas.credentials import Credential
from keystone.platform.identity.services.access import AccessContext


@pytest.fixture
def file_settings(empty_database_url, tmp_path, monkeypatch):
    return make_file_settings(empty_database_url, tmp_path, monkeypatch)


@pytest.fixture
def multi_client(file_settings, s3):
    file_settings.file_storages = file_settings.file_storages | {"cloud": cloud_config(s3)}
    initialize_database(file_settings)
    with open_client(file_settings) as client:
        yield client


@pytest.mark.postgres
def test_source_routing_independent_files_and_pinned_replays(multi_client, s3):
    client = multi_client
    options = client.get("/api/v1/_test/materials/options").json()
    assert options["storage_keys"] == ["local", "cloud"]
    assert not client.app.state.runtime.storage.shared_files
    assert "fixture-secret" not in str(options)
    assert upload(client, storage_key="unknown").json()["code"] == "storage_unknown"
    local = upload(client).json()
    cloud = upload(client, key="backup-one", storage_key="cloud").json()
    assert local["storage_key"] == "local" and cloud["storage_key"] == "cloud"
    assert local["id"] != cloud["id"]
    assert upload(client, storage_key="cloud").json()["code"] == "idempotency_conflict"
    assert upload(client, key="backup-one").json()["id"] == cloud["id"]
    assert upload(client, b"other", key="backup-one").json()["code"] == "idempotency_conflict"
    client.app.state.runtime.storage.default_key = "cloud"
    assert upload(client).json()["id"] == local["id"]
    # Resolve the original source before applying a default source's smaller body limit.
    client.app.state.runtime.settings.file_max_bytes = 128 * 1024**2
    oversized_declaration = upload(client, expected_size=65 * 1024**2)
    assert oversized_declaration.status_code == 422
    assert oversized_declaration.json()["code"] == "file_size_mismatch"
    assert upload(client, key="new-default").json()["storage_key"] == "cloud"
    for file in (local, cloud):
        assert client.get(f"/api/v1/_test/materials/{file['id']}/content").content == b"hello"
        assert "object_key" not in file and "storage_fingerprint" not in file
    headers = bearer(token(client, ["test_files.asset:inspect"])["token"])
    assert (
        client.get(f"/api/v1/_test/materials/{cloud['id']}/content", headers=headers).status_code
        == 403
    )
    assert client.delete(f"/api/v1/_test/materials/{cloud['id']}").status_code == 202
    clean(client)
    assert client.get(f"/api/v1/_test/materials/{cloud['id']}").json()["status"] == "deleted"
    assert client.get(f"/api/v1/_test/materials/{local['id']}/content").content == b"hello"


@pytest.mark.postgres
def test_s3_delete_retries_and_sweeps_tombstone(multi_client, s3):
    client = multi_client
    identifier = UUID(upload(client, storage_key="cloud").json()["id"])
    (key,) = s3.objects
    path = f"/api/v1/_test/materials/{identifier}"
    assert client.delete(path).status_code == 202
    s3.unavailable = True
    clean(client)
    assert client.get(path).json()["status"] == "deleting"
    s3.unavailable = False
    age_file(client, identifier, timedelta(minutes=2))  # Wait for the retry delay.
    clean(client)
    assert not s3.objects
    # Simulate late content appearing after its registered tombstone was committed.
    s3.objects[key] = b"hello"
    age_file(client, identifier)
    clean(client)
    assert not s3.objects
    assert client.get(path).json()["status"] == "deleted"


@pytest.mark.postgres
def test_missing_or_retargeted_source_never_uses_default(multi_client, s3):
    client = multi_client
    file = upload(client, storage_key="cloud").json()
    registry = client.app.state.runtime.storage
    source = registry.sources.pop("cloud")
    assert client.get(f"/api/v1/_test/materials/{file['id']}/content").status_code == 503
    registry.sources["cloud"] = Storage(source.config.model_copy(update={"prefix": "other"}))
    calls = len(s3.calls)
    assert client.get(f"/api/v1/_test/materials/{file['id']}/content").status_code == 503
    assert upload(client, storage_key="cloud").status_code == 503
    client.delete(f"/api/v1/_test/materials/{file['id']}")
    clean(client)
    assert len(s3.calls) == calls and s3.objects


@pytest.fixture
def s3_registries(file_settings, s3, tmp_path, monkeypatch):
    settings = file_settings.model_copy(
        update={
            "file_storages": {"cloud": cloud_config(s3)},
            "file_default_storage": "cloud",
        }
    )
    initialize_database(settings)
    other = StorageRegistry(
        settings.model_copy(update={"file_coordination_root": tmp_path / "node2"})
    )
    other.initialize()

    def unexpected_local_lease(*args, **kwargs):
        pytest.fail("S3-only file operations must not acquire local file locks")

    monkeypatch.setattr(other, "lease", unexpected_local_lease)
    with open_client(settings) as client:
        storage = client.app.state.runtime.storage
        assert storage.shared_files and other.shared_files
        monkeypatch.setattr(storage, "lease", unexpected_local_lease)
        yield client, other


def age_file(client, identifier, delta=timedelta(hours=2)):
    query(
        client,
        lambda session: session.execute(
            update(File).where(File.id == identifier).values(updated_at=utcnow() - delta)
        ),
    )


def terminate_file_lease(client, identifier):
    key = lock_key(f"keystone.files:{identifier}")

    async def terminate(session):
        pid = await session.scalar(
            text(
                "SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND granted "
                "AND database = (SELECT oid FROM pg_database WHERE datname = current_database()) "
                "AND classid::bigint = :high AND objid::bigint = :low AND objsubid = 1 "
                "AND mode = 'ExclusiveLock'"
            ),
            {
                "high": (key >> 32) & 0xFFFFFFFF,
                "low": key & 0xFFFFFFFF,
            },
        )
        assert pid is not None
        assert await session.scalar(text("SELECT pg_terminate_backend(:pid, 5000)"), {"pid": pid})

    query(client, terminate)


def delay_first_write(monkeypatch, source):
    write = source.write
    entered, release = Event(), Event()

    def delayed_first_write(*args):
        if not entered.is_set():
            entered.set()
            assert release.wait(15)
        return write(*args)

    monkeypatch.setattr(source, "write", delayed_first_write)
    return entered, release


@pytest.mark.postgres
def test_postgres_lost_upload_cannot_activate_or_restart(s3_registries, s3, monkeypatch):
    client, other = s3_registries
    runtime = client.app.state.runtime
    entered, release = delay_first_write(monkeypatch, runtime.storage.get())
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(upload, client)
        try:
            assert entered.wait(15)
            (uploading,) = stored_files(client)
            identifier = UUID(uploading["id"])
            age_file(client, identifier)
            client.portal.call(cleanup, runtime.database, other)
            assert stored_files(client)[0]["status"] == "uploading"
            assert upload(client).json()["code"] == "file_busy"

            terminate_file_lease(client, identifier)
            assert upload(client).json()["code"] == "upload_unavailable"
            replacement = upload(client, key="replacement")
            assert replacement.status_code == 201, replacement.text
            replacement_id = replacement.json()["id"]
            assert replacement_id != str(identifier)
            (replacement_key,) = s3.objects
        finally:
            release.set()
        assert pending.result(timeout=15).json()["code"] == "file_coordination_lost"

    assert len(s3.objects) == 2, "The disconnected writer's PUT landed after takeover"

    def status(session):
        return session.scalar(select(File.status).where(File.id == identifier))

    assert query(client, status) == "uploading"
    client.portal.call(cleanup, runtime.database, other)
    assert query(client, status) == "failed"
    assert s3.objects == {replacement_key: b"hello"}
    assert client.get(f"/api/v1/_test/materials/{replacement_id}/content").content == b"hello"


@pytest.mark.postgres
def test_postgres_late_residue_delete_cannot_touch_new_attempt_or_settle_twice(s3_registries, s3):
    client, other = s3_registries
    runtime = client.app.state.runtime
    s3.fail_put = True
    assert upload(client).status_code == 503
    (failed,) = stored_files(client)
    assert failed["status"] == "failed"
    identifier = UUID(failed["id"])
    (old_path,) = s3.objects
    s3.fail_put = False
    age_file(client, identifier)

    entered, release = Event(), Event()

    def delay_first_delete():
        if not entered.is_set():
            entered.set()
            assert release.wait(15)

    s3.before_delete = delay_first_delete

    def residue_state():
        return query(
            client,
            lambda session: session.execute(
                select(File.residue_checks, File.updated_at).where(File.id == identifier)
            ),
        ).one()

    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(clean, client)
        try:
            assert entered.wait(15)
            terminate_file_lease(client, identifier)
            assert upload(client).json()["code"] == "upload_unavailable"
            replacement = upload(client, key="replacement")
            assert replacement.status_code == 201, replacement.text
            replacement_id = replacement.json()["id"]
            client.portal.call(cleanup, runtime.database, other)
            settled = residue_state()
            assert settled.residue_checks == 1 and old_path not in s3.objects
        finally:
            release.set()
        with pytest.raises(ServiceError) as lost:
            pending.result(timeout=15)
        assert lost.value.code == "file_coordination_lost"

    assert residue_state() == settled
    assert len([call for call in s3.calls if call[:2] == ("DELETE", old_path)]) == 2
    assert client.get(f"/api/v1/_test/materials/{replacement_id}/content").content == b"hello"


@pytest.mark.postgres
def test_filesystem_lost_writer_keeps_physical_lock_until_same_key_can_retry(client, monkeypatch):
    runtime = client.app.state.runtime
    entered, release = delay_first_write(monkeypatch, runtime.storage.get())
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(upload, client)
        try:
            assert entered.wait(15)
            (uploading,) = stored_files(client)
            identifier = UUID(uploading["id"])
            terminate_file_lease(client, identifier)
            assert upload(client).json()["code"] == "file_busy"
            age_file(client, identifier)
            clean(client)
            assert stored_files(client)[0]["status"] == "uploading"
        finally:
            release.set()
        assert pending.result(timeout=15).json()["code"] == "file_coordination_lost"

    clean(client)
    assert stored_files(client)[0]["status"] == "failed"
    retry = upload(client)
    assert retry.status_code == 201 and retry.json()["id"] == str(identifier)
    assert client.get(f"/api/v1/_test/materials/{identifier}/content").content == b"hello"


@pytest.mark.postgres
def test_filesystem_borrower_blocks_cleanup_with_physical_lock(client):
    runtime = client.app.state.runtime
    identity = AccessContext(
        runtime.database,
        runtime.settings,
        Credential("session", client.cookies["keystone_session"]),
    )
    service = FileService(identity, runtime.storage)
    identifier = UUID(upload(client).json()["id"])
    path = f"/api/v1/_test/materials/{identifier}"
    entered, release = Event(), Event()

    async def borrow():
        async with service.local_file(identifier) as local:
            entered.set()
            assert await asyncio.to_thread(release.wait, 15)
            assert local.path.read_bytes() == b"hello"

    async def request_delete():
        # Accept deletion using the normal row-locked service transaction; the test
        # fixture's HTTP maintenance route also takes an exclusive physical lock.
        async with identity.access("test_files.asset:manage") as (session, auth):
            await FileReferenceService(identity).release_in_transaction(
                session, auth, {(f"test_files:{identifier}", "material", identifier)}
            )
            await service.request_delete_in_transaction(session, auth, identifier)

    pending = client.portal.start_task_soon(borrow)
    try:
        assert entered.wait(15)
        client.portal.call(request_delete)
        key = lock_key(f"keystone.files:{identifier}")
        assert query(client, lambda s: s.scalar(select(func.pg_try_advisory_xact_lock(key))))
        clean(client)
        assert client.get(path).json()["status"] == "deleting"
    finally:
        release.set()
    pending.result(timeout=15)
    clean(client)
    assert client.get(path).json()["status"] == "deleted"

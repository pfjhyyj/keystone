from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy import update
from support.storage_support import cloud_config
from support.storage_support import s3 as s3
from test_files_dictionary import clean, stored_files, upload
from test_files_dictionary import client as client
from test_identity import bearer, query, token

from keystone.core.time import utcnow
from keystone.platform.files.adapters.storage import Storage, StorageRegistry
from keystone.platform.files.models import File


@pytest.fixture
def multi_client(client, s3):
    runtime = client.app.state.runtime
    runtime.settings.file_storages = {
        **runtime.settings.file_storages,
        "cloud": cloud_config(s3),
    }
    runtime.storage = StorageRegistry(runtime.settings)
    yield client


@pytest.mark.postgres
def test_source_routing_independent_files_and_pinned_replays(multi_client, s3):
    client = multi_client
    options = client.get("/api/v1/_test/materials/options").json()
    assert options["storage_keys"] == ["local", "cloud"]
    assert "fixture-secret" not in str(options)
    assert upload(client, storage_key="unknown").json()["code"] == "storage_unknown"
    local = upload(client).json()
    cloud = upload(client, key="backup-one", storage_key="cloud").json()
    assert local["storage_key"] == "local" and cloud["storage_key"] == "cloud"
    assert local["id"] != cloud["id"]
    assert upload(client, storage_key="cloud").json()["code"] == "idempotency_conflict"
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
def test_s3_unknown_write_retry_and_late_write_cleanup(multi_client, s3):
    client = multi_client
    s3.fail_put = True
    assert upload(client, storage_key="cloud").status_code == 503
    failed = stored_files(client)[0]
    assert failed["status"] == "failed" and s3.objects
    s3.fail_put = False
    assert upload(client, storage_key="cloud").json()["id"] == failed["id"]
    assert upload(client, b"other", storage_key="cloud").json()["code"] == "idempotency_conflict"
    client.delete(f"/api/v1/_test/materials/{failed['id']}")
    s3.unavailable = True
    clean(client)
    assert client.get(f"/api/v1/_test/materials/{failed['id']}").json()["status"] == "deleting"
    s3.unavailable = False
    clean(client)
    assert not s3.objects
    # Simulate a request accepted before a crashed writer returning after DELETE.
    s3.objects[f"/backups/business-a/files/{UUID(failed['id']).hex}"] = b"hello"

    async def age(session):
        await session.execute(
            update(File)
            .where(File.id == UUID(failed["id"]))
            .values(updated_at=utcnow() - timedelta(hours=2))
        )

    query(client, age)
    clean(client)
    assert not s3.objects
    assert client.get(f"/api/v1/_test/materials/{failed['id']}").json()["status"] == "deleted"


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

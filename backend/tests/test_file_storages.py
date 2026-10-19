from datetime import timedelta
from uuid import UUID

import pytest
from pydantic import ValidationError
from sqlalchemy import update
from storage_support import s3_endpoint
from test_files_dictionary import clean, upload
from test_files_dictionary import client as client
from test_identity import bearer, query, token

from keystone.core.config import Settings
from keystone.core.errors import ServiceError
from keystone.core.storage_config import StorageConfig
from keystone.core.time import utcnow
from keystone.platform.files.leases import initialize
from keystone.platform.files.models import File
from keystone.platform.files.storage import Storage, StorageRegistry


@pytest.fixture
def s3():
    with s3_endpoint() as state:
        yield state


def cloud_config(s3, **changes):
    return StorageConfig(
        driver="s3",
        endpoint=s3.endpoint,
        bucket="backups",
        prefix="business-a",
        access_key="fixture-access",
        secret_key="fixture-secret",
        **changes,
    )


def test_configuration_validation_and_secret_redaction(tmp_path, s3, monkeypatch):
    monkeypatch.setenv("KEYSTONE_FILE_STORAGES", cloud_config(s3).model_dump_json())
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
    monkeypatch.delenv("KEYSTONE_FILE_STORAGES")
    sources = {"local": {"driver": "filesystem", "root": str(tmp_path)}, "cloud": cloud_config(s3)}
    settings = Settings(_env_file=None, file_storages=sources, file_coordination_root=tmp_path)
    assert set(settings.file_storages) == {"local", "cloud"}
    assert settings.file_coordination_root == tmp_path
    assert "fixture-secret" not in repr(settings)
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            file_storages=sources,
            file_default_storage="missing",
            file_coordination_root=tmp_path,
        )
    with pytest.raises(ValidationError, match="FILE_COORDINATION_ROOT"):
        Settings(_env_file=None, file_storages=sources)
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None, file_storages={"cloud": cloud_config(s3)}, file_default_storage="cloud"
        )
    cloud_only = Settings(
        _env_file=None,
        file_storages={"cloud": cloud_config(s3)},
        file_default_storage="cloud",
        file_coordination_root=tmp_path,
    )
    assert set(cloud_only.file_storages) == {"cloud"}
    for changes in (
        {"prefix": "../bad"},
        {"endpoint": "http://user:secret@localhost"},
        {"access_key": ""},
        {"endpoint": "https://example.test/path"},
    ):
        values = cloud_config(s3).model_dump()
        values.update(changes)
        with pytest.raises(ValidationError) as rejected:
            StorageConfig(**values)
        assert "fixture-secret" not in str(rejected.value)


async def test_coordination_directory_does_not_create_a_storage_source(tmp_path):
    registry = StorageRegistry(Settings(_env_file=None, file_coordination_root=tmp_path))
    assert registry.sources == {}
    assert await registry.readiness() == ("unconfigured", {})
    with pytest.raises(ServiceError):
        registry.get()
    with pytest.raises(ServiceError, match="KEYSTONE_FILE_STORAGES") as missing:
        registry.initialize()
    assert missing.value.code == "storage_unavailable"
    assert not list(tmp_path.iterdir())


async def test_initialize_uses_explicit_coordination_and_content_directories(tmp_path):
    registry = StorageRegistry(
        Settings(
            _env_file=None,
            file_coordination_root=tmp_path / "coordination",
            file_storages={"local": {"driver": "filesystem", "root": tmp_path / "content"}},
        )
    )
    registry.initialize()
    registry.initialize()
    assert registry.coordination.root == tmp_path / "coordination"
    assert registry.get().root == tmp_path / "content"
    assert await registry.readiness() == ("ok", {"local": "ok"})


@pytest.mark.parametrize("driver", ["filesystem", "s3"])
def test_real_opendal_storage_contract(tmp_path, s3, driver):
    initialize(tmp_path / "local")
    config = (
        StorageConfig(driver="filesystem", root=tmp_path / "local")
        if driver == "filesystem"
        else cloud_config(s3)
    )
    storage = Storage(config)
    fingerprint, key, other = storage.fingerprint(), "files/" + "1" * 32, "files/" + "2" * 32
    storage.ready()
    for target in (key, other):
        storage.write(fingerprint, target, b"hello")
    assert storage.read(fingerprint, key, 5) == b"hello"
    with pytest.raises(ServiceError):
        storage.read(fingerprint, key, 4)
    for unsafe in ("files/../secret", "files/" + "z" * 32, "/files/" + "1" * 32):
        with pytest.raises(ServiceError):
            storage.delete(fingerprint, unsafe)
    storage.delete(fingerprint, key)
    storage.delete(fingerprint, key)
    assert storage.read(fingerprint, other, 5) == b"hello"
    if driver == "s3":
        assert all(auth.startswith("AWS4-HMAC-SHA256 ") for _, _, auth in s3.calls)
        assert "/backups/business-a/" in next(iter(s3.objects))


def test_fingerprint_pins_location_and_allows_credential_rotation(s3):
    config = cloud_config(s3)
    fingerprint = Storage(config).fingerprint()
    assert Storage(config.model_copy(update={"secret_key": "rotated"})).fingerprint() == fingerprint
    for field, value in (
        ("endpoint", "https://another.test"),
        ("bucket", "other"),
        ("region", "other"),
        ("prefix", "other"),
    ):
        changed = Storage(config.model_copy(update={field: value}))
        with pytest.raises(ServiceError):
            changed.delete(fingerprint, "files/" + "1" * 32)
    assert not s3.calls  # Identity mismatch is rejected before network access.


async def test_auxiliary_source_failure_does_not_gate_default(tmp_path, s3):
    initialize(tmp_path)
    registry = StorageRegistry(
        Settings(
            _env_file=None,
            file_coordination_root=tmp_path,
            file_storages={
                "local": StorageConfig(driver="filesystem", root=tmp_path),
                "cloud": cloud_config(s3),
            },
        )
    )
    s3.unavailable = True
    status, sources = await registry.readiness()
    assert status == "ok" and sources == {"local": "ok", "cloud": "unavailable"}


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
    options = client.get("/api/v1/files/options").json()
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
    assert upload(client, key="new-default").json()["storage_key"] == "cloud"
    for file in (local, cloud):
        assert client.get(f"/api/v1/files/{file['id']}/content").content == b"hello"
        assert "object_key" not in file and "storage_fingerprint" not in file
    headers = bearer(token(client, ["file:inspect"])["token"])
    assert client.get(f"/api/v1/files/{cloud['id']}/content", headers=headers).status_code == 403
    assert client.delete(f"/api/v1/files/{cloud['id']}").status_code == 202
    clean(client)
    assert client.get(f"/api/v1/files/{cloud['id']}").json()["status"] == "deleted"
    assert client.get(f"/api/v1/files/{local['id']}/content").content == b"hello"


@pytest.mark.postgres
def test_s3_unknown_write_retry_and_late_write_cleanup(multi_client, s3):
    client = multi_client
    s3.fail_put = True
    assert upload(client, storage_key="cloud").status_code == 503
    failed = client.get("/api/v1/files").json()[0]
    assert failed["status"] == "failed" and s3.objects
    s3.fail_put = False
    assert upload(client, storage_key="cloud").json()["id"] == failed["id"]
    assert upload(client, b"other", storage_key="cloud").json()["code"] == "idempotency_conflict"
    client.delete(f"/api/v1/files/{failed['id']}")
    s3.unavailable = True
    clean(client)
    assert client.get(f"/api/v1/files/{failed['id']}").json()["status"] == "deleting"
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
    assert client.get(f"/api/v1/files/{failed['id']}").json()["status"] == "deleted"
    assert client.get(f"/api/v1/files/{failed['id']}/grants").json() == []


@pytest.mark.postgres
def test_missing_or_retargeted_source_never_uses_default(multi_client, s3):
    client = multi_client
    file = upload(client, storage_key="cloud").json()
    registry = client.app.state.runtime.storage
    source = registry.sources.pop("cloud")
    assert client.get(f"/api/v1/files/{file['id']}/content").status_code == 503
    registry.sources["cloud"] = Storage(source.config.model_copy(update={"prefix": "other"}))
    calls = len(s3.calls)
    assert client.get(f"/api/v1/files/{file['id']}/content").status_code == 503
    assert upload(client, storage_key="cloud").status_code == 503
    client.delete(f"/api/v1/files/{file['id']}")
    clean(client)
    assert len(s3.calls) == calls and s3.objects

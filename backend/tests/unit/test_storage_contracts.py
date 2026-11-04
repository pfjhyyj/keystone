"""Storage drivers and configuration contracts without domain transactions."""

import pytest
from pydantic import ValidationError
from support.storage_support import cloud_config
from support.storage_support import s3 as s3

from keystone.core.config import Settings
from keystone.core.errors import ServiceError
from keystone.core.storage_config import StorageConfig
from keystone.platform.files.adapters.storage import Storage, StorageRegistry, initialize


def test_configuration_validation_and_secret_redaction(tmp_path, s3, monkeypatch):
    monkeypatch.setenv("KEYSTONE_FILE_STORAGES__LOCAL__ROOT", str(tmp_path))
    with pytest.raises(ValidationError):
        Settings(config_file=None)
    monkeypatch.delenv("KEYSTONE_FILE_STORAGES__LOCAL__ROOT")
    sources = {"local": {"driver": "filesystem", "root": str(tmp_path)}, "cloud": cloud_config(s3)}
    settings = Settings(config_file=None, file_storages=sources, file_coordination_root=tmp_path)
    assert set(settings.file_storages) == {"local", "cloud"}
    assert settings.file_coordination_root == tmp_path
    assert "fixture-secret" not in repr(settings)
    with pytest.raises(ValidationError):
        Settings(
            config_file=None,
            file_storages=sources,
            file_default_storage="missing",
            file_coordination_root=tmp_path,
        )
    with pytest.raises(ValidationError, match="file_coordination_root"):
        Settings(config_file=None, file_storages=sources)
    with pytest.raises(ValidationError):
        Settings(
            config_file=None,
            file_storages={"cloud": cloud_config(s3)},
            file_default_storage="cloud",
        )
    cloud_only = Settings(
        config_file=None,
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
    registry = StorageRegistry(Settings(config_file=None, file_coordination_root=tmp_path))
    assert registry.sources == {}
    assert await registry.readiness() == ("unconfigured", {})
    with pytest.raises(ServiceError):
        registry.get()
    with pytest.raises(ServiceError, match="file_storages") as missing:
        registry.initialize()
    assert missing.value.code == "storage_unavailable"
    assert not list(tmp_path.iterdir())


async def test_initialize_uses_explicit_coordination_and_content_directories(tmp_path):
    registry = StorageRegistry(
        Settings(
            config_file=None,
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
            config_file=None,
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

from pathlib import Path

import pytest
from pydantic import ValidationError

from keystone.core.config import Settings
from keystone.platform.files.adapters.storage import StorageRegistry


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    import os

    for name in os.environ:
        if name.startswith("KEYSTONE_"):
            monkeypatch.delenv(name)


def write_config(directory):
    path = directory / "keystone.toml"
    path.write_text(
        """port = 8100
file_coordination_root = "coordination"
file_default_storage = "local"
browser_origins = ["http://localhost:5173"]
[file_storages.local]
driver = "filesystem"
root = "local files"
[file_storages.cloud]
driver = "s3"
endpoint = "https://s3.example.test"
bucket = "documents"
access_key = "test-access"
secret_key = "test-secret"
""",
        encoding="utf-8",
    )
    return path


async def test_default_toml_and_environment_overrides(tmp_path, monkeypatch):
    write_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KEYSTONE_PORT", "8200")
    monkeypatch.setenv("KEYSTONE_FILE_STORAGES__CLOUD__SECRET_KEY", "rotated-secret")
    monkeypatch.setenv("KEYSTONE_FILE_STORAGES__LOCAL__ROOT", "overridden files")
    settings = Settings()
    assert settings.port == 8200
    assert settings.browser_origins == ["http://localhost:5173"]
    assert settings.file_coordination_root == tmp_path / "coordination"
    assert settings.file_storages["local"].root == tmp_path / "overridden files"
    assert settings.file_storages["cloud"].bucket == "documents"
    assert settings.file_storages["cloud"].secret_key.get_secret_value() == "rotated-secret"
    assert "rotated-secret" not in repr(settings)
    registry = StorageRegistry(settings)
    registry.initialize()
    local = registry.get()
    fingerprint, key = local.fingerprint(), "files/" + "1" * 32
    local.write(fingerprint, key, b"hello")
    assert local.read(fingerprint, key, 5) == b"hello"
    assert Settings(port=8300).port == 8300


def test_explicit_config_path_and_relative_overrides(tmp_path, monkeypatch):
    config = write_config(tmp_path)
    monkeypatch.chdir(tmp_path.parent)
    monkeypatch.setenv("KEYSTONE_CONFIG_FILE", str(config))
    monkeypatch.setenv("KEYSTONE_FILE_COORDINATION_ROOT", "overridden coordination")
    settings = Settings()
    assert settings.port == 8100
    assert settings.file_storages["local"].root == tmp_path / "local files"
    assert settings.file_coordination_root == tmp_path / "overridden coordination"
    assert Settings(config_file=None).file_storages == {}


def test_backend_dotenv_is_no_longer_loaded(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("KEYSTONE_PORT=8300\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert Settings().port == 8000


@pytest.mark.parametrize(
    "values",
    [
        {"worker_id": "../invalid-worker"},
        {"worker_ids": ["keystone-worker"]},
        {"flow_worker_concurrency": 5, "flow_global_concurrency": 4},
        {"model_inference_concurrency": 2, "model_scope_concurrency": 3},
    ],
)
def test_fixed_worker_identity_and_shared_capacity_reject_invalid_configuration(values):
    with pytest.raises(ValidationError):
        Settings(config_file=None, **values)


def test_worker_identity_is_independent_of_shared_config_digest():
    from keystone.core.worker_identity import config_digest

    original = Settings(config_file=None)
    extra = Settings(config_file=None, worker_id="keystone-worker-02")
    assert config_digest(original) == config_digest(extra)


def test_worker_shared_config_digest_checks_storage_secrets_without_exposing_them(tmp_path):
    from pydantic import SecretStr

    from keystone.core.worker_identity import config_digest

    settings = Settings(config_file=write_config(tmp_path))
    before = config_digest(settings)
    settings.worker_id = "keystone-worker-02"
    settings.port = 8300
    assert config_digest(settings) == before
    settings.file_storages["cloud"].secret_key = SecretStr("different-storage-secret")
    assert config_digest(settings) != before
    assert "different-storage-secret" not in config_digest(settings)


def test_file_capacity_defaults_preserve_platform_configuration():
    settings = Settings(config_file=None)
    assert settings.file_min_free_bytes == 1024**3
    assert settings.file_max_bytes == 16 * 1024**2
    assert settings.browser_origins == [
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "http://127.0.0.1:8080",
        "http://localhost:8080",
    ]


@pytest.mark.parametrize("floor", [-1, 1024**4 + 1])
def test_invalid_file_capacity_floor_is_rejected(floor):
    with pytest.raises(ValidationError):
        Settings(config_file=None, file_min_free_bytes=floor)


@pytest.mark.parametrize("floor", [0, 1024**4])
def test_file_capacity_floor_toml_environment_and_worker_digest(tmp_path, monkeypatch, floor):
    from keystone.core.worker_identity import config_digest

    config = tmp_path / "keystone.toml"
    config.write_text("file_min_free_bytes = 512\n", encoding="utf-8")
    original = Settings(config_file=config)
    assert original.file_min_free_bytes == 512
    monkeypatch.setenv("KEYSTONE_FILE_MIN_FREE_BYTES", str(floor))
    overridden = Settings(config_file=config)
    assert overridden.file_min_free_bytes == floor
    assert config_digest(overridden) != config_digest(original)


def test_removed_flow_toggle_is_rejected():
    with pytest.raises(ValidationError):
        Settings(config_file=None, flow_enabled=True)


def test_example_is_complete_and_uses_config_directory(monkeypatch, tmp_path):
    example = Path(__file__).parents[2] / "keystone.toml.example"
    monkeypatch.chdir(tmp_path)
    settings = Settings(config_file=example)
    assert settings.database_url is not None
    assert settings.file_storages["local"].root == example.parent.parent / ".data/files"
    assert settings.file_coordination_root == settings.file_storages["local"].root
    assert settings.initial_admin_password is None


@pytest.mark.parametrize(
    "content",
    ['port = "secret-invalid-port"', 'unknown_field = "secret-value"'],
)
def test_invalid_toml_fields_reject_without_exposing_values(tmp_path, content):
    config = tmp_path / "keystone.toml"
    config.write_text(content, encoding="utf-8")
    with pytest.raises(ValidationError) as rejected:
        Settings(config_file=config)
    assert "secret-invalid-port" not in str(rejected.value)
    assert "secret-value" not in str(rejected.value)


def test_missing_explicit_file_and_invalid_toml_are_errors(tmp_path):
    config = tmp_path / "keystone.toml"
    with pytest.raises(ValueError, match="does not exist"):
        Settings(config_file=config)
    config.write_text("[broken", encoding="utf-8")
    with pytest.raises(ValueError):
        Settings(config_file=config)


def test_explicit_default_filename_must_exist(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KEYSTONE_CONFIG_FILE", "keystone.toml")
    with pytest.raises(ValueError, match="does not exist"):
        Settings()

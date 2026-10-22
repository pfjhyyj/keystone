from pathlib import Path

import pytest
from pydantic import ValidationError

from keystone.core.config import Settings
from keystone.platform.files.storage import StorageRegistry


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


def test_example_is_complete_and_uses_config_directory(monkeypatch, tmp_path):
    example = Path(__file__).parents[1] / "keystone.toml.example"
    monkeypatch.chdir(tmp_path)
    settings = Settings(config_file=example)
    assert settings.database_url is not None
    assert settings.file_storages["local"].root == example.parent.parent / ".data/files"
    assert settings.file_coordination_root == settings.file_storages["local"].root
    assert settings.flow_enabled
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

import os
import re
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict, TomlConfigSettingsSource
from sqlalchemy.engine import make_url

from keystone.core.storage_config import StorageConfig, StorageKey


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="KEYSTONE_",
        env_nested_delimiter="__",
        extra="forbid",
        hide_input_in_errors=True,
    )

    config_file: Path | None = Field(default=Path("keystone.toml"), exclude=True)
    env: Literal["development", "test", "production"] = "development"
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    database_url: SecretStr | None = None
    file_coordination_root: Path | None = None
    file_storages: dict[StorageKey, StorageConfig] = Field(default_factory=dict, repr=False)
    file_default_storage: StorageKey = "local"
    file_max_bytes: int = Field(default=16 * 1024 * 1024, ge=1, le=1024**4)
    file_transfer_slots: int = Field(default=4, ge=1, le=32)
    file_temp_bytes: int = Field(default=64 * 1024**3, ge=1, le=32 * 1024**4)
    file_min_free_bytes: int = Field(default=1024**3, ge=0, le=1024**4)
    file_upload_timeout_seconds: int = Field(default=3600, ge=1, le=86400)
    file_unreferenced_grace_seconds: int | None = Field(default=None, ge=1, le=31536000)
    flow_worker_concurrency: int = Field(default=2, ge=1, le=16)
    flow_global_concurrency: int = Field(default=4, ge=1, le=64)
    worker_id: str = "keystone-worker"
    worker_shutdown_seconds: int = Field(default=10, ge=1, le=600)
    model_cost_concurrency: int = Field(default=2, ge=1, le=8)
    model_inference_concurrency: int = Field(default=4, ge=1, le=32)
    model_scope_concurrency: int = Field(default=4, ge=1, le=16)
    model_send_interval_ms: int = Field(default=0, ge=0, le=60000)
    model_cooldown_seconds: int = Field(default=5, ge=1, le=300)
    model_cooldown_max_seconds: int = Field(default=300, ge=1, le=3600)
    initial_admin_password: SecretStr | None = Field(default=None, repr=False)
    session_hours: int = Field(default=8, ge=1, le=168)
    pat_max_days: int = Field(default=90, ge=1, le=365)
    history_cleanup_enabled: bool = False
    session_retention_days: int = Field(default=30, ge=1, le=3650)
    pat_retention_days: int = Field(default=90, ge=1, le=3650)
    audit_retention_days: int = Field(default=365, ge=1, le=3650)
    maintenance_retention_days: int = Field(default=30, ge=1, le=3650)
    browser_origins: list[str] = [
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "http://127.0.0.1:8080",
        "http://localhost:8080",
    ]

    @classmethod
    def settings_customise_sources(
        cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings
    ):
        path = init_settings.init_kwargs.get(
            "config_file", os.environ.get("KEYSTONE_CONFIG_FILE", "keystone.toml")
        )
        explicit = (
            "config_file" in init_settings.init_kwargs or "KEYSTONE_CONFIG_FILE" in os.environ
        )
        if explicit and path is not None and not Path(path).is_file():
            raise ValueError("Configured keystone.toml file does not exist")
        source = TomlConfigSettingsSource(settings_cls, toml_file=path)
        if "config_file" in source():
            raise ValueError("Set the config file path through KEYSTONE_CONFIG_FILE")
        return init_settings, env_settings, source

    @model_validator(mode="before")
    @classmethod
    def resolve_file_paths(cls, values):
        values = dict(values)
        config = values.get("config_file", "keystone.toml")
        base = Path(config).resolve().parent if config is not None else Path.cwd()

        def absolute(value):
            return (base / value).resolve() if isinstance(value, (str, Path)) else value

        if values.get("file_coordination_root") is not None:
            values["file_coordination_root"] = absolute(values["file_coordination_root"])
        sources = values.get("file_storages")
        if isinstance(sources, dict):
            values["file_storages"] = {
                key: {**source, "root": absolute(source["root"])}
                if isinstance(source, dict) and source.get("root") is not None
                else source
                for key, source in sources.items()
            }
        return values

    @model_validator(mode="after")
    def valid_workers(self):
        if (
            not re.fullmatch(r"[a-z][a-z0-9_-]{2,63}", self.worker_id)
            or self.flow_worker_concurrency > self.flow_global_concurrency
            or self.model_scope_concurrency > self.model_inference_concurrency
            or self.model_cooldown_seconds > self.model_cooldown_max_seconds
        ):
            raise ValueError(
                "Configure a valid fixed worker identity and shared concurrency limits"
            )
        return self

    @model_validator(mode="after")
    def valid_storage_sources(self):
        if self.file_storages and self.file_default_storage not in self.file_storages:
            raise ValueError("Default file storage must name a configured source")
        if self.file_storages and self.file_coordination_root is None:
            raise ValueError("Configured file storage requires file_coordination_root")
        return self

    @model_validator(mode="after")
    def secure_production_origins(self):
        from urllib.parse import urlsplit

        for origin in self.browser_origins:
            parsed = urlsplit(origin)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.path
                or parsed.query
                or parsed.fragment
                or parsed.username
                or (self.env == "production" and parsed.scheme != "https")
            ):
                raise ValueError("Configure exact browser origins; production requires HTTPS")
        if not self.browser_origins:
            raise ValueError("Configure at least one browser origin")
        return self

    @field_validator("initial_admin_password", mode="before")
    @classmethod
    def empty_initial_password(cls, value):
        return None if value == "" else value

    @field_validator("database_url")
    @classmethod
    def postgres_only(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        try:
            url = make_url(value.get_secret_value())
            if url.drivername != "postgresql+psycopg" or not url.database:
                raise ValueError
        except Exception:
            raise ValueError("Use a postgresql+psycopg URL with an explicit database") from None
        return value

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

from keystone.core.storage_config import StorageConfig, StorageKey


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="KEYSTONE_", env_file=".env", extra="ignore", hide_input_in_errors=True
    )

    env: Literal["development", "test", "production"] = "development"
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    database_url: SecretStr | None = None
    worker_ready_file: Path | None = None
    file_coordination_root: Path | None = None
    file_storages: dict[StorageKey, StorageConfig] = Field(default_factory=dict, repr=False)
    file_default_storage: StorageKey = "local"
    file_max_bytes: int = Field(default=16 * 1024 * 1024, ge=1, le=64 * 1024 * 1024)
    initial_admin_password: SecretStr | None = Field(default=None, repr=False)
    flow_enabled: bool = True
    session_hours: int = Field(default=8, ge=1, le=168)
    pat_max_days: int = Field(default=90, ge=1, le=365)
    browser_origins: list[str] = [
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "http://127.0.0.1:8080",
        "http://localhost:8080",
    ]

    @model_validator(mode="after")
    def valid_storage_sources(self):
        if self.file_storages and self.file_default_storage not in self.file_storages:
            raise ValueError("Default file storage must name a configured source")
        if self.file_storages and self.file_coordination_root is None:
            raise ValueError("Configured file storage requires FILE_COORDINATION_ROOT")
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

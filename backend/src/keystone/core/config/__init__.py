"""Single settings loader shared by the API, Worker, and CLI."""

import os
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict, TomlConfigSettingsSource
from sqlalchemy.engine import make_url

from keystone.core.config.browser import BrowserRuntimeSettings
from keystone.core.config.execution import ExecutionSettings
from keystone.core.config.files import FileSettings, normalize_file_paths
from keystone.core.config.identity import IdentitySettings
from keystone.core.config.maintenance import MaintenanceSettings
from keystone.core.config.model_access import ModelAccessSettings


# Domain models contribute flat fields; only this root reads deployment configuration.
class Settings(
    BaseSettings,
    FileSettings,
    ExecutionSettings,
    ModelAccessSettings,
    IdentitySettings,
    MaintenanceSettings,
    BrowserRuntimeSettings,
):
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
        return normalize_file_paths(values, base)

    @model_validator(mode="after")
    def valid_browser_backup_storage(self):
        if self.browser_backup_key is not None:
            storage = self.file_storages.get(self.file_default_storage)
            if (
                self.file_coordination_root is None
                or storage is None
                or storage.driver != "filesystem"
            ):
                raise ValueError(
                    "browser backup requires the default file storage to use local filesystem"
                )
        return self

    @model_validator(mode="after")
    def secure_production_origins(self):
        self.validate_browser_origins(production=self.env == "production")
        return self

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

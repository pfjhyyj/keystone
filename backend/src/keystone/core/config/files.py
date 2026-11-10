import os
from pathlib import Path

from pydantic import BaseModel, Field, model_validator

from keystone.core.storage_config import StorageConfig, StorageKey


class FileSettings(BaseModel):
    file_coordination_root: Path | None = None
    file_storages: dict[StorageKey, StorageConfig] = Field(default_factory=dict, repr=False)
    file_default_storage: StorageKey = "local"
    file_max_bytes: int = Field(default=16 * 1024 * 1024, ge=1, le=1024**4)
    file_min_free_bytes: int = Field(default=1024**3, ge=0, le=1024**4)
    file_upload_timeout_seconds: int = Field(default=3600, ge=1, le=86400)
    file_unreferenced_grace_seconds: int | None = Field(default=None, ge=1, le=31536000)

    @model_validator(mode="after")
    def valid_storage_sources(self):
        if self.file_storages and self.file_default_storage not in self.file_storages:
            raise ValueError("Default file storage must name a configured source")
        if self.file_storages and self.file_coordination_root is None:
            raise ValueError("Configured file storage requires file_coordination_root")
        return self


def normalize_file_paths(values, base):
    if any(
        name in os.environ for name in ("KEYSTONE_FILE_TRANSFER_SLOTS", "KEYSTONE_FILE_TEMP_BYTES")
    ):
        raise ValueError("Remove retired file slot and temporary-total environment settings")

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

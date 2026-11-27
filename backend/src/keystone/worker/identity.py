"""Trusted process identity, never supplied by a public model request."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from pydantic import SecretStr


@dataclass(frozen=True)
class WorkerIdentity:
    worker_id: str
    instance_id: UUID
    execution_version: str


def config_digest(settings):
    # Identity and HTTP-only options differ by process; shared execution declarations do not.
    fields = (
        "flow_worker_concurrency",
        "flow_global_concurrency",
        "model_cost_concurrency",
        "model_inference_concurrency",
        "model_scope_concurrency",
        "model_send_interval_ms",
        "model_cooldown_seconds",
        "model_cooldown_max_seconds",
        "file_coordination_root",
        "file_storages",
        "file_default_storage",
        "file_max_bytes",
        "file_min_free_bytes",
        "file_upload_timeout_seconds",
        "file_unreferenced_grace_seconds",
        "history_cleanup_enabled",
        "audit_cleanup_enabled",
        "flow_diagnostic_cleanup_enabled",
        "maintenance_cleanup_enabled",
        "session_retention_days",
        "pat_retention_days",
        "audit_retention_days",
        "flow_diagnostic_retention_days",
        "maintenance_retention_days",
        "message_delivery_retention_days",
        "opensandbox_url",
        "opensandbox_key",
        "browser_runtime_image",
    )

    def serialize(value):
        if isinstance(value, SecretStr):
            return value.get_secret_value()
        if isinstance(value, Path):
            return str(value)
        raise TypeError("Unsupported shared configuration value")

    data = settings.model_dump(include=set(fields))
    if settings.s3_only_storage:
        del data["file_coordination_root"], data["file_min_free_bytes"]
    if settings.credential_key is not None:
        data["credential_key"] = settings.credential_key
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":"), default=serialize).encode()
    ).hexdigest()

"""Trusted process identity, never supplied by a public model request."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from pydantic import SecretStr

from keystone.core.advisory import lock_key

DEPLOYMENT_LOCK = lock_key("keystone-worker-deployment")


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
        "file_transfer_slots",
        "file_temp_bytes",
        "file_min_free_bytes",
        "file_upload_timeout_seconds",
    )

    def serialize(value):
        if isinstance(value, SecretStr):
            return value.get_secret_value()
        if isinstance(value, Path):
            return str(value)
        raise TypeError("Unsupported shared configuration value")

    data = settings.model_dump(include=set(fields))
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":"), default=serialize).encode()
    ).hexdigest()

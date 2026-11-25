from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from keystone.core.schemas import Input
from keystone.platform.flow.schemas.errors import ERRORS
from keystone.platform.flow.schemas.snapshots import public_snapshot, read_snapshot
from keystone.platform.model_access.schemas.types import Key


class Start(Input):
    definition_key: str = Field(min_length=1, max_length=100)
    request_key: str = Field(min_length=1, max_length=128)
    input: dict
    definition_revision: str | None = Field(None, min_length=1, max_length=64)
    model_bindings: dict[str, Key] = Field(default_factory=dict, max_length=16)


def view(row):
    values = {
        key: getattr(row, key)
        for key in (
            "id",
            "definition_key",
            "definition_snapshot",
            "execution_version",
            "actor_id",
            "personal_access_token_id",
            "request_key",
            "request_digest",
            "input",
            "created_at",
            "status",
            "result",
            "error_code",
            "cancel_requested_at",
            "finished_at",
            "observed_at",
        )
    }
    snapshot = read_snapshot(row.definition_snapshot)
    values["definition_snapshot"] = public_snapshot(snapshot)
    values["definition_revision"] = snapshot["revision"]
    values["model_bindings"] = {
        key: binding["model_key"] for key, binding in snapshot["model_bindings"].items()
    }
    return {k: v.astimezone(UTC) if isinstance(v, datetime) else v for k, v in values.items()}


class RunOutcome(BaseModel):
    """Business-readable end of a Run; no result, message, credential or provider text."""

    model_config = ConfigDict(frozen=True)

    status: Literal["pending", "running", "succeeded", "failed", "cancelled"]
    error_code: str | None = None
    # A failed/cancelled Run whose model request may have been sent and billed without a
    # recoverable result; a new Run would send it again.
    model_result_unknown: bool = False


def outcome(status, error_code, *, unresolved=False):
    if status in {"pending", "running", "succeeded"}:
        return RunOutcome(status=status)
    code = error_code if error_code is None or error_code in ERRORS else "flow_execution_failed"
    return RunOutcome(
        status=status,
        error_code=code,
        model_result_unknown=unresolved or code == "model_result_unavailable",
    )

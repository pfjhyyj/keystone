from datetime import UTC, datetime

from pydantic import Field

from keystone.core.schemas import Input


class Start(Input):
    definition_key: str = Field(min_length=1, max_length=100)
    request_key: str = Field(min_length=1, max_length=128)
    input: dict


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
    return {k: v.astimezone(UTC) if isinstance(v, datetime) else v for k, v in values.items()}

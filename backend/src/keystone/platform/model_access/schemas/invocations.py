from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from keystone.core.schemas import Input

from .types import Key, ModelName


class Message(Input):
    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=32000)


class Invoke(Input):
    model_key: Key
    messages: list[Message] = Field(min_length=1, max_length=32)
    max_tokens: int = Field(default=1024, ge=1, le=4096)
    stream: bool = False

    @model_validator(mode="after")
    def bounded_input(self):
        if sum(len(m.content.encode()) for m in self.messages) > 65536:
            raise ValueError("Messages exceed 64 KiB")
        return self


class Selection(Input):
    """Trusted internal selection only. Public callers submit a model_key."""

    model_entry_id: UUID
    channel_id: UUID
    provider_model: ModelName
    api_base: str


class FlowContext(Input):
    flow_run_id: UUID
    node_key: str = Field(min_length=1, max_length=100)
    agent_key: str | None = Field(None, min_length=1, max_length=100)
    agent_call_id: UUID | None = None


def invocation_view(row):
    values = {
        key: getattr(row, key)
        for key in (
            "id",
            "user_id",
            "flow_context",
            "model_entry_id",
            "channel_id",
            "provider_model",
            "upstream_request_id",
            "status",
            "finished_at",
            "usage",
            "cost_amount",
            "cost_unit",
            "cost_source",
            "cost_note",
            "error_code",
            "created_at",
            "updated_at",
        )
    }
    values["cost_amount"] = format(row.cost_amount, "f") if row.cost_amount is not None else None
    return values

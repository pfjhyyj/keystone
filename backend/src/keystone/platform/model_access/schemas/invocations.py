from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from keystone.core.schemas import Input

from .operations import Delivery, Operation
from .types import Key, ModelName


class Message(Input):
    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=32000)


class TextRequest(Input):
    messages: list[Message] = Field(min_length=1, max_length=32)
    max_tokens: int = Field(default=1024, ge=1, le=4096)

    @model_validator(mode="after")
    def bounded_input(self):
        if sum(len(m.content.encode()) for m in self.messages) > 65536:
            raise ValueError("Messages exceed 64 KiB")
        return self


class Invoke(TextRequest):
    model_key: Key
    stream: bool = False


class TextResult(Input):
    invocation_id: UUID
    content: str = Field(max_length=4 * 1024 * 1024)


class Selection(Input):
    """Trusted internal selection only. Public callers submit a model_key."""

    model_entry_id: UUID
    channel_id: UUID
    provider_model: ModelName
    api_base: str
    provider: str | None = None
    operation: Operation = "text.generate"
    delivery: Delivery = "sync"


class ResolvedModelBinding(Input):
    model_key: Key
    selection: Selection


class InvocationContext(Input):
    flow_run_id: UUID
    node_key: str = Field(min_length=1, max_length=100)
    model_role: str | None = Field(None, pattern=r"^[a-z][a-z0-9_]{0,63}$")
    agent_key: str | None = Field(None, min_length=1, max_length=100)
    agent_call_id: UUID | None = None
    workflow_id: str | None = Field(None, min_length=1, max_length=512)
    step_id: int | None = Field(None, strict=True, ge=0, le=2**31 - 1)

    def authority(self):
        """Observational Step position is not part of original task authority."""
        return self.model_dump(mode="json", exclude_none=True, exclude={"workflow_id", "step_id"})


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
            "operation",
            "upstream_request_id",
            "status",
            "finished_at",
            "usage",
            "cost_amount",
            "cost_unit",
            "cost_source",
            "cost_note",
            "cost_check_status",
            "cost_check_reason",
            "cost_check_rounds",
            "cost_check_errors",
            "cost_checked_at",
            "cost_next_check_at",
            "cost_deadline_at",
            "error_code",
            "created_at",
            "updated_at",
        )
    }
    values["cost_amount"] = format(row.cost_amount, "f") if row.cost_amount is not None else None
    return values

"""Shared, bounded filters for history, summaries and analytics."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator

from keystone.core.schemas import Input

from .operations import Operation

Dimension = Literal[
    "channel",
    "model",
    "upstream_model",
    "user",
    "source",
    "flow_run",
    "node",
    "model_role",
    "agent",
    "operation",
    "status",
    "cost_status",
    "cost_source",
    "error",
]


class InvocationFilters(Input):
    since: datetime | None = None
    until: datetime | None = None
    channel_id: UUID | None = None
    model_entry_id: UUID | None = None
    provider_model: str | None = Field(None, min_length=1, max_length=128)
    user_id: UUID | Literal["system"] | None = None
    flow_run_id: UUID | None = None
    node_key: str | None = Field(None, min_length=1, max_length=100)
    model_role: str | None = Field(None, min_length=1, max_length=64)
    agent_key: str | None = Field(None, min_length=1, max_length=100)
    agent_call_id: UUID | None = None
    operation: Operation | None = None
    source: Literal["direct", "flow", "agent"] | None = None
    status: Literal["running", "succeeded", "failed", "unknown"] | None = None
    cost_status: Literal["pending", "confirmed", "blocked", "unavailable", "exhausted"] | None = (
        None
    )
    cost_source: Literal["response", "query", "unknown"] | None = None
    cost_known: bool | None = None
    cost_unit: str | None = Field(None, min_length=1, max_length=32)
    error_code: str | None = Field(None, min_length=1, max_length=64)
    missing: str | None = Field(None, max_length=100)

    @field_validator("missing")
    @classmethod
    def missing_dimensions(cls, value):
        if value is not None and (
            not set(value.split(",")) <= {"flow_run", "node", "model_role", "agent", "error"}
            or len(value.split(",")) != len(set(value.split(",")))
        ):
            raise ValueError("Invalid missing dimensions")
        return value

    def filters(self):
        return self.model_dump(include=set(InvocationFilters.model_fields))


class InvocationListQuery(InvocationFilters):
    offset: int = Field(0, ge=0, le=100000)
    limit: int = Field(50, ge=1, le=100)


class StatisticsQuery(InvocationListQuery):
    group_by: Dimension = "channel"
    order_by: Literal["calls", "cost"] = "calls"
    sort_unit: str = Field("USD", min_length=1, max_length=32)
    bucket: Literal["hour", "day", "month"] = "day"


class StatisticsOptionsQuery(InvocationListQuery):
    dimension: Dimension = "channel"
    q: str = Field("", max_length=128)

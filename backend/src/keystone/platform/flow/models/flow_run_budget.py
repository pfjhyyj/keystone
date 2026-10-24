from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from keystone.core.database import Base

COUNTER_FIELDS = (
    "model_requests",
    "tool_calls",
    "iterations",
    "file_count",
    "file_bytes",
    "input_tokens",
    "output_tokens",
    "unreported_text_requests",
)
LIMIT_FIELDS = (
    "model_requests",
    "tool_calls",
    "iterations",
    "file_count",
    "file_bytes",
    "input_tokens",
    "output_tokens",
    "concurrent_operations",
    "temporary_bytes",
)


def nonnegative(column, fields):
    required = "ARRAY[" + ",".join("'" + key + "'" for key in fields) + "]"
    return (
        f"jsonb_typeof({column}) = 'object' AND octet_length({column}::text) <= 4096 "
        f"AND {column} ?& {required} AND {column} - {required} = '{{}}'::jsonb AND "
        + " AND ".join(
            f"(jsonb_typeof({column}->'{key}') = 'number' AND ({column}->>'{key}')::numeric >= 0)"
            for key in fields
        )
    )


class FlowRunBudget(Base):
    __tablename__ = "flow_run_budgets"
    __table_args__ = (
        CheckConstraint(nonnegative("limits", LIMIT_FIELDS), name="flow_budget_limits"),
        CheckConstraint(nonnegative("counters", COUNTER_FIELDS), name="flow_budget_counters"),
    )
    run_id: Mapped[UUID] = mapped_column(ForeignKey("keystone.flow_runs.id"), primary_key=True)
    limits: Mapped[dict] = mapped_column(JSONB)
    counters: Mapped[dict] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

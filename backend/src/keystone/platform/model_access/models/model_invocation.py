from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Numeric, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from keystone.core.database import Base


class ModelInvocation(Base):
    __tablename__ = "model_invocations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running','succeeded','failed','unknown')", name="invocation_status"
        ),
        CheckConstraint("(status = 'running') = (finished_at IS NULL)", name="invocation_finished"),
        CheckConstraint(
            "(cost_amount IS NULL AND cost_unit IS NULL AND cost_source = 'unknown') OR "
            "(cost_amount IS NOT NULL AND cost_amount >= 0 AND cost_unit IS NOT NULL "
            "AND cost_source IN ('response','query'))",
            name="invocation_cost",
        ),
        CheckConstraint(
            "result_context IS NULL OR (jsonb_typeof(result_context) = 'object' "
            "AND octet_length(result_context::text) <= 16384)",
            name="invocation_result_context",
        ),
        CheckConstraint(
            "estimate_status IN ('pending','estimated','partial','unavailable','invalid')",
            name="invocation_estimate_status",
        ),
        CheckConstraint(
            "(estimate_status = 'estimated' AND estimate_amount IS NOT NULL "
            "AND estimate_amount >= 0 AND estimate_amount < 1e20 "
            "AND estimate_currency IS NOT NULL) OR "
            "(estimate_status <> 'estimated' AND estimate_amount IS NULL)",
            name="invocation_estimate_amount",
        ),
        *(
            CheckConstraint(
                f"{field} IS NULL OR (jsonb_typeof({field}) = 'object' "
                f"AND octet_length({field}::text) <= {limit})",
                name=f"invocation_{field}",
            )
            for field, limit in (
                ("estimate_snapshot", 24576),
                ("estimate_facts", 8192),
                ("estimate_detail", 16384),
            )
        ),
        Index("ix_model_invocations_user_time", "user_id", "created_at", "id"),
        Index("ix_model_invocations_time", "created_at", "id"),
        Index(
            "ix_model_invocations_flow_time",
            text("(flow_context ->> 'flow_run_id')"),
            "created_at",
            "id",
        ),
        CheckConstraint(
            "cost_check_status IN ('pending','confirmed','blocked','unavailable','exhausted')",
            name="invocation_cost_check_status",
        ),
        CheckConstraint(
            "cost_check_rounds >= 0 AND cost_check_errors >= 0", name="invocation_cost_counts"
        ),
        Index("ix_model_invocations_cost_due", "cost_check_status", "cost_next_check_at", "id"),
        Index("ix_invocation_worker_running", "worker_id", "status"),
        CheckConstraint(
            "(worker_id IS NULL AND worker_instance_id IS NULL AND execution_version IS NULL) OR "
            "(worker_id IS NOT NULL AND worker_instance_id IS NOT NULL "
            "AND execution_version IS NOT NULL)",
            name="invocation_worker_owner",
        ),
    )
    operation: Mapped[str] = mapped_column(
        String(32), default="text.generate", server_default="text.generate"
    )
    result_context: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    worker_id: Mapped[str | None] = mapped_column(String(64))
    worker_instance_id: Mapped[UUID | None] = mapped_column()
    execution_version: Mapped[str | None] = mapped_column(String(64))
    user_id: Mapped[UUID | None] = mapped_column(ForeignKey("keystone.users.id"))
    flow_context: Mapped[dict | None] = mapped_column(JSONB)
    model_entry_id: Mapped[UUID] = mapped_column(ForeignKey("keystone.model_entries.id"))
    channel_id: Mapped[UUID] = mapped_column(ForeignKey("keystone.model_channels.id"))
    provider_model: Mapped[str] = mapped_column(String(128))
    upstream_request_id: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(16), default="running")
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    usage: Mapped[dict | None] = mapped_column(JSONB)
    estimate_snapshot: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    estimate_facts: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    estimate_status: Mapped[str] = mapped_column(
        String(16), default="unavailable", server_default="unavailable"
    )
    estimate_amount: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    estimate_currency: Mapped[str | None] = mapped_column(String(3))
    estimate_reason: Mapped[str | None] = mapped_column(
        String(64), default="snapshot_missing", server_default="snapshot_missing"
    )
    estimate_detail: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    estimate_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cost_amount: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    cost_unit: Mapped[str | None] = mapped_column(String(32))
    cost_source: Mapped[str] = mapped_column(String(16), default="unknown")
    cost_note: Mapped[str | None] = mapped_column(String(100))
    error_code: Mapped[str | None] = mapped_column(String(64))
    cost_check_status: Mapped[str] = mapped_column(
        String(16), default="unavailable", server_default="unavailable"
    )
    cost_check_reason: Mapped[str | None] = mapped_column(String(64))
    cost_check_rounds: Mapped[int] = mapped_column(default=0, server_default="0")
    cost_check_errors: Mapped[int] = mapped_column(default=0, server_default="0")
    cost_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cost_next_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cost_deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cost_context: Mapped[dict | None] = mapped_column(JSONB)
    receipt_ids: Mapped[dict | None] = mapped_column(JSONB)
    cost_evidence: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Numeric, String, func
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
        Index("ix_model_invocations_user_time", "user_id", "created_at", "id"),
        Index("ix_model_invocations_time", "created_at", "id"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID | None] = mapped_column(ForeignKey("keystone.users.id"))
    flow_context: Mapped[dict | None] = mapped_column(JSONB)
    model_entry_id: Mapped[UUID] = mapped_column(ForeignKey("keystone.model_entries.id"))
    channel_id: Mapped[UUID] = mapped_column(ForeignKey("keystone.model_channels.id"))
    provider_model: Mapped[str] = mapped_column(String(128))
    upstream_request_id: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(16), default="running")
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    usage: Mapped[dict | None] = mapped_column(JSONB)
    cost_amount: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    cost_unit: Mapped[str | None] = mapped_column(String(32))
    cost_source: Mapped[str] = mapped_column(String(16), default="unknown")
    cost_note: Mapped[str | None] = mapped_column(String(100))
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

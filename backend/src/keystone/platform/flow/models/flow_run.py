from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from keystone.core.database import Base

TERMINAL = {"succeeded", "failed", "cancelled"}


class FlowRun(Base):
    __tablename__ = "flow_runs"
    __table_args__ = (
        UniqueConstraint("actor_id", "request_key", name="flow_request_key"),
        CheckConstraint(
            "status IN ('pending','running','succeeded','failed','cancelled')", name="flow_status"
        ),
        CheckConstraint(
            "(status IN ('succeeded','failed','cancelled')) = (finished_at IS NOT NULL)",
            name="flow_finished",
        ),
        CheckConstraint("(status = 'succeeded') = (result IS NOT NULL)", name="flow_result"),
        Index("ix_flow_scan", "status", "observed_at", "id"),
        Index("ix_flow_owner", "actor_id", "created_at", "id"),
        Index("ix_flow_reconcile_attempt", "status", "reconcile_attempted_at", "id"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    definition_key: Mapped[str] = mapped_column(String(100))
    definition_snapshot: Mapped[dict] = mapped_column(JSONB)
    execution_version: Mapped[str] = mapped_column(String(64))
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("keystone.users.id"))
    personal_access_token_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("keystone.personal_access_tokens.id")
    )
    request_key: Mapped[str] = mapped_column(String(128))
    request_digest: Mapped[str] = mapped_column(String(64))
    input: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    status: Mapped[str] = mapped_column(String(16), default="pending")
    result: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    error_code: Mapped[str | None] = mapped_column(String(100))
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reconcile_attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

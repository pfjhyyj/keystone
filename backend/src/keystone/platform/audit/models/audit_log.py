from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from keystone.core.database import Base


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        CheckConstraint("actor_type IN ('user', 'system', 'anonymous')"),
        CheckConstraint("(actor_type = 'user') = (actor_id IS NOT NULL)"),
        CheckConstraint("outcome IN ('success', 'failure', 'denied')"),
        Index("ix_audit_time", "occurred_at", "id"),
        Index("ix_audit_actor_time", "actor_id", "occurred_at"),
        Index("ix_audit_target_time", "target_type", "target_id", "occurred_at"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    actor_type: Mapped[str] = mapped_column(String(16))
    actor_id: Mapped[UUID | None]
    action: Mapped[str] = mapped_column(String(100))
    target_type: Mapped[str | None] = mapped_column(String(100))
    target_id: Mapped[str | None] = mapped_column(String(128))
    outcome: Mapped[str] = mapped_column(String(16))
    correlation_id: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict | None] = mapped_column(JSONB)

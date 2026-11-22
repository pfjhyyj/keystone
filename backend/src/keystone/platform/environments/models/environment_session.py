from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from keystone.core.database import Base


class EnvironmentSession(Base):
    __tablename__ = "environment_sessions"
    __table_args__ = (
        CheckConstraint(
            "state IN ('starting', 'ready', 'stopping', 'review', 'stopped', 'failed')",
            name="ck_environment_session_state",
        ),
        CheckConstraint(
            "purpose IN ('interactive', 'backup')",
            name="ck_environment_session_purpose",
        ),
        Index(
            "uq_environment_session_occupied",
            "environment_id",
            unique=True,
            postgresql_where=text("state IN ('starting', 'ready', 'stopping', 'review')"),
        ),
        Index("ix_environment_session_history", "environment_id", "created_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    environment_id: Mapped[UUID] = mapped_column(ForeignKey("keystone.environments.id"))
    purpose: Mapped[str] = mapped_column(String(16))
    state: Mapped[str] = mapped_column(String(16))
    sandbox_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    stop_requested: Mapped[bool]
    delete_sent: Mapped[bool]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

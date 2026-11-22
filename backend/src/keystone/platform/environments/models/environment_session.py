from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, String, func, text
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
        CheckConstraint(
            "(control_owner IS NULL) = (control_kind IS NULL)"
            " AND (control_owner IS NULL) = (control_until IS NULL)"
            " AND (control_kind IS NULL OR control_kind IN ('human', 'automation'))",
            name="ck_environment_session_control",
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
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # A start, stop, check or backup in flight in some process; expires if that process dies.
    operation_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # One input holder across API and Worker: a screen connection or an automation run.
    control_owner: Mapped[UUID | None]
    control_kind: Mapped[str | None] = mapped_column(String(16))
    control_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    control_generation: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from keystone.core.database import Base


class BrowserRun(Base):
    __tablename__ = "browser_runs"
    __table_args__ = (
        CheckConstraint(
            "state IN ('starting', 'ready', 'stopping', 'review', 'stopped', 'failed')",
            name="ck_browser_run_state",
        ),
        CheckConstraint(
            "purpose IN ('browse', 'backup')",
            name="ck_browser_run_purpose",
        ),
        Index(
            "uq_browser_run_occupied",
            "workspace_id",
            unique=True,
            postgresql_where=text("state IN ('starting', 'ready', 'stopping', 'review')"),
        ),
        Index("ix_browser_run_history", "workspace_id", "created_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("keystone.browser_workspaces.id"))
    purpose: Mapped[str] = mapped_column(String(16))
    state: Mapped[str] = mapped_column(String(16))
    sandbox_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    stop_requested: Mapped[bool]
    delete_sent: Mapped[bool]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

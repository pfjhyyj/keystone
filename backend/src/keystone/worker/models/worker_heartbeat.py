from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from keystone.core.database import Base


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"
    __table_args__ = (
        Index("ix_worker_identity_start", "worker_id", "started_at"),
        CheckConstraint(
            "state IN ('starting', 'ready', 'unready', 'draining', 'stopped')",
            name="worker_heartbeat_state",
        ),
    )

    instance_id: Mapped[UUID] = mapped_column(primary_key=True)
    host_id: Mapped[str] = mapped_column(String(255))
    worker_id: Mapped[str] = mapped_column(String(64), server_default="keystone-worker")
    execution_version: Mapped[str | None] = mapped_column(String(64))
    config_digest: Mapped[str | None] = mapped_column(String(64))
    coordination_id: Mapped[str | None] = mapped_column(String(64))
    task_results: Mapped[dict] = mapped_column(JSONB, server_default="{}")
    state: Mapped[str] = mapped_column(String(16))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

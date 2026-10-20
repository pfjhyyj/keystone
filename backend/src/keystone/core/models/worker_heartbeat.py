from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from keystone.core.database import Base


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"
    __table_args__ = (
        CheckConstraint(
            "state IN ('starting', 'ready', 'unready', 'stopped')", name="worker_heartbeat_state"
        ),
    )

    instance_id: Mapped[UUID] = mapped_column(primary_key=True)
    host_id: Mapped[str] = mapped_column(String(255))
    state: Mapped[str] = mapped_column(String(16))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

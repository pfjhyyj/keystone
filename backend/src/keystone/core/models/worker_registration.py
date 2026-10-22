from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from keystone.core.database import Base


class WorkerRegistration(Base):
    __tablename__ = "worker_registrations"
    __table_args__ = (
        CheckConstraint("state IN ('registered','retired')", name="worker_registration_state"),
        CheckConstraint("(state = 'retired') = (retired_at IS NOT NULL)", name="worker_retired"),
    )
    worker_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    state: Mapped[str] = mapped_column(String(16), server_default="registered")
    host_id: Mapped[str | None] = mapped_column(String(255))
    coordination_id: Mapped[str | None] = mapped_column(String(64))
    execution_version: Mapped[str | None] = mapped_column(String(64))
    config_digest: Mapped[str | None] = mapped_column(String(64))
    instance_id: Mapped[UUID | None] = mapped_column()
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

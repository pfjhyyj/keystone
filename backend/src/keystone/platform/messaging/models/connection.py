from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from keystone.core.database import Base


class MessageConnection(Base):
    """One Worker's long connection to one app, refreshed while the Worker runs.

    Rows from Workers that stopped are not removed by anyone else; readers only trust rows
    refreshed recently.
    """

    __tablename__ = "message_connections"
    __table_args__ = (
        CheckConstraint(
            "state IN ('connecting', 'connected', 'refused', 'lost')",
            name="ck_message_connection_state",
        ),
    )

    channel_id: Mapped[UUID] = mapped_column(
        ForeignKey("keystone.message_channels.id"), primary_key=True
    )
    worker_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    state: Mapped[str] = mapped_column(String(16))
    error_code: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

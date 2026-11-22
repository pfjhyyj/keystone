from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from keystone.core.database import Base


class MessageDestination(Base):
    __tablename__ = "message_destinations"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    # Businesses address destinations by this fixed key, never by provider IDs.
    key: Mapped[str] = mapped_column(String(64), unique=True)
    channel_id: Mapped[UUID] = mapped_column(ForeignKey("keystone.message_channels.id"), index=True)
    external_id: Mapped[str | None] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(128))
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_by: Mapped[UUID | None]
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_by: Mapped[UUID | None]

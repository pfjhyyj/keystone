from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from keystone.core.database import Base

STATUSES = ("pending", "cancelled", "sending", "succeeded", "failed", "unknown", "expired")


class MessageDelivery(Base):
    __tablename__ = "message_deliveries"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'cancelled', 'sending', 'succeeded', 'failed', 'unknown',"
            " 'expired')",
            name="ck_message_delivery_status",
        ),
        CheckConstraint("kind IN ('text', 'card')", name="ck_message_delivery_kind"),
        Index(
            "ix_message_delivery_open",
            "status",
            "created_at",
            postgresql_where=text("status IN ('pending', 'sending')"),
        ),
        Index("ix_message_delivery_history", "created_at", "id"),
    )

    # The ID is also the provider de-duplication key where one exists.
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    channel_id: Mapped[UUID] = mapped_column(ForeignKey("keystone.message_channels.id"))
    destination_id: Mapped[UUID] = mapped_column(ForeignKey("keystone.message_destinations.id"))
    source_module: Mapped[str] = mapped_column(String(48))
    source_type: Mapped[str] = mapped_column(String(64))
    source_id: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(8))
    content: Mapped[dict] = mapped_column(JSONB)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    attempts: Mapped[int] = mapped_column(default=0)
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_by: Mapped[UUID | None]
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

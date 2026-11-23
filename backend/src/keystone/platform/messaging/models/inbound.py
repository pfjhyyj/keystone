from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from keystone.core.database import Base


class MessageInbound(Base):
    """One event received from a channel: a card action or a binding message."""

    __tablename__ = "message_inbounds"
    __table_args__ = (
        UniqueConstraint("channel_id", "dedupe_key", name="uq_message_inbound_dedupe"),
        CheckConstraint(
            "status IN ('received', 'processed', 'rejected', 'expired', 'failed')",
            name="ck_message_inbound_status",
        ),
        CheckConstraint("kind IN ('card_action', 'bind')", name="ck_message_inbound_kind"),
        Index(
            "ix_message_inbound_open",
            "updated_at",
            postgresql_where=text("status = 'received'"),
        ),
        Index("ix_message_inbound_history", "received_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    channel_id: Mapped[UUID] = mapped_column(ForeignKey("keystone.message_channels.id"))
    # Message ID for messages (Feishu may push one twice), event ID for card callbacks.
    dedupe_key: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16))
    # Card actions: the delivery whose card was clicked. Not a foreign key, so delivery
    # retention never waits on inbound history.
    delivery_id: Mapped[UUID | None]
    action_key: Mapped[str | None] = mapped_column(String(64))
    provider_message_id: Mapped[str | None] = mapped_column(String(64))
    external_user_id: Mapped[str] = mapped_column(String(64))
    actor_id: Mapped[UUID | None]
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="received")
    attempts: Mapped[int] = mapped_column(default=0)
    error_code: Mapped[str | None] = mapped_column(String(64))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

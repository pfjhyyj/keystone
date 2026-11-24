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
        # A delivery goes to a configured destination, one bound user or a command's
        # conversation, never more than one.
        CheckConstraint(
            "num_nonnulls(destination_id, recipient_user_id, reply_inbound_id) = 1",
            name="ck_message_delivery_target",
        ),
        CheckConstraint(
            "(content_mode = 'stored' AND content IS NOT NULL)"
            " OR (content_mode = 'supplied' AND content IS NULL)",
            name="ck_message_delivery_content",
        ),
        # The platform answers a failed command at most once.
        Index(
            "uq_message_delivery_failure_reply",
            "reply_inbound_id",
            unique=True,
            postgresql_where=text("source_module = 'messaging'"),
        ),
        Index(
            "ix_message_delivery_open",
            "status",
            "created_at",
            postgresql_where=text("status IN ('pending', 'sending')"),
        ),
        Index("ix_message_delivery_history", "created_at", "id"),
        Index(
            "ix_message_delivery_retry",
            "first_attempted_at",
            postgresql_where=text("status = 'unknown' AND attempts < 3"),
        ),
    )

    # The ID is also the provider de-duplication key where one exists.
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    channel_id: Mapped[UUID] = mapped_column(ForeignKey("keystone.message_channels.id"))
    destination_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("keystone.message_destinations.id")
    )
    # Resolved to the user's binding on this channel when sent, so unbinding stops it.
    recipient_user_id: Mapped[UUID | None]
    # Replies go to the command's message. Not a foreign key, so inbound retention never
    # waits on delivery history.
    reply_inbound_id: Mapped[UUID | None]
    source_module: Mapped[str] = mapped_column(String(48))
    source_type: Mapped[str] = mapped_column(String(64))
    source_id: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(8))
    # `supplied` content is fetched from the business right before each send, never stored.
    content_mode: Mapped[str] = mapped_column(String(8), default="stored")
    content: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    attempts: Mapped[int] = mapped_column(default=0)
    error_code: Mapped[str | None] = mapped_column(String(64))
    provider_message_id: Mapped[str | None] = mapped_column(String(64))
    # Retries with the same ID stay inside the provider de-duplication window from here.
    first_attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_by: Mapped[UUID | None]
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

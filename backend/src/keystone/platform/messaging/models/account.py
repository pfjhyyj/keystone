from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from keystone.core.database import Base


class ExternalAccount(Base):
    """A Keystone user's identity in one app channel; open IDs differ per app."""

    __tablename__ = "message_external_accounts"
    __table_args__ = (
        UniqueConstraint("channel_id", "external_user_id", name="uq_message_account_external"),
        UniqueConstraint("channel_id", "user_id", name="uq_message_account_user"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    channel_id: Mapped[UUID] = mapped_column(ForeignKey("keystone.message_channels.id"))
    external_user_id: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[UUID]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_by: Mapped[UUID | None]

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from keystone.core.database import Base


class Channel(Base):
    __tablename__ = "model_channels"
    __table_args__ = (
        CheckConstraint(
            "provider IN ('deepseek','openrouter','aliyun_bailian')", name="channel_provider"
        ),
        CheckConstraint(
            "balance_check_status IN ('never','ok','error','unavailable')",
            name="channel_balance_status",
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(128))
    provider: Mapped[str] = mapped_column(String(32))
    api_base: Mapped[str] = mapped_column(String(512))
    credential_ref: Mapped[str] = mapped_column(String(64))
    billing_credential_ref: Mapped[str | None] = mapped_column(String(64))
    upstream_account_ref: Mapped[str | None] = mapped_column(String(128))
    is_active: Mapped[bool] = mapped_column(default=True)
    balance_snapshot: Mapped[dict | None] = mapped_column(JSONB)
    balance_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    balance_last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    balance_check_status: Mapped[str] = mapped_column(String(16), default="never")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_by: Mapped[UUID | None]
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_by: Mapped[UUID | None]

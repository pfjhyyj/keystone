from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from keystone.core.database import Base


class ModelEntry(Base):
    __tablename__ = "model_entries"
    __table_args__ = (
        CheckConstraint("pricing_revision >= 0", name="model_entry_pricing_revision"),
        CheckConstraint(
            "jsonb_typeof(pricing) = 'object' AND octet_length(pricing::text) <= 262144",
            name="model_entry_pricing",
        ),
        CheckConstraint(
            "jsonb_typeof(operations) = 'array' AND operations <> '[]'::jsonb",
            name="model_entry_operations",
        ),
    )
    operations: Mapped[list[str]] = mapped_column(
        JSONB,
        default=lambda: ["text.generate"],
        server_default=text("'[\"text.generate\"]'::jsonb"),
    )
    pricing: Mapped[dict] = mapped_column(
        JSONB,
        default=lambda: {"mode": "inherit", "rules": []},
        server_default=text('\'{"mode":"inherit","rules":[]}\'::jsonb'),
    )
    pricing_revision: Mapped[int] = mapped_column(default=0, server_default="0")
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    key: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(128), default="", server_default="")
    channel_id: Mapped[UUID] = mapped_column(ForeignKey("keystone.model_channels.id"), index=True)
    provider_model: Mapped[str] = mapped_column(String(128))
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_by: Mapped[UUID | None]
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_by: Mapped[UUID | None]

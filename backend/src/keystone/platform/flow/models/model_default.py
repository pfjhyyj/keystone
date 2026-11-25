from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from keystone.core.database import Base


class FlowModelDefault(Base):
    """Model used for a Flow role when the starting request leaves it unbound."""

    __tablename__ = "flow_model_defaults"
    __table_args__ = (Index("ix_flow_model_default_entry", "model_entry_id"),)
    definition_key: Mapped[str] = mapped_column(String(100), primary_key=True)
    role: Mapped[str] = mapped_column(String(64), primary_key=True)
    model_entry_id: Mapped[UUID] = mapped_column(
        ForeignKey("keystone.model_entries.id", ondelete="CASCADE")
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_by: Mapped[UUID | None]

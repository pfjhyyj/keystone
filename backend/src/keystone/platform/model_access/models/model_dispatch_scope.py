from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from keystone.core.database import Base


class ModelDispatchScope(Base):
    __tablename__ = "model_dispatch_scopes"
    __table_args__ = (CheckConstraint("scope ~ '^[0-9a-f]{64}$'", name="dispatch_scope_identity"),)
    scope: Mapped[str] = mapped_column(String(64), primary_key=True)
    next_send_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    blocked_until: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

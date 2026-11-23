from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from keystone.core.database import Base


class MessageBindCode(Base):
    """A user's one live self-binding code; only its digest is kept."""

    __tablename__ = "message_bind_codes"

    user_id: Mapped[UUID] = mapped_column(primary_key=True)
    code_digest: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, String, false
from sqlalchemy.orm import Mapped, mapped_column

from keystone.core.database import Base


class BrowserWorkspace(Base):
    __tablename__ = "browser_workspaces"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("keystone.users.id"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    volume: Mapped[str] = mapped_column(String(128), unique=True)
    backup_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    backup_failed: Mapped[bool] = mapped_column(server_default=false())

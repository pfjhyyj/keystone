from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, UniqueConstraint, false
from sqlalchemy.orm import Mapped, mapped_column

from keystone.core.database import Base


class Environment(Base):
    __tablename__ = "environments"
    __table_args__ = (
        CheckConstraint("kind IN ('chromium')", name="ck_environment_kind"),
        UniqueConstraint("owner_id", "kind", "key", name="uq_environment_owner_kind_key"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("keystone.users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    key: Mapped[str] = mapped_column(String(64))
    volume: Mapped[str] = mapped_column(String(128), unique=True)
    # Only an environment that has never been ready may initialize an empty volume.
    state_initialization_pending: Mapped[bool] = mapped_column(server_default=false())
    backup_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    backup_failed: Mapped[bool] = mapped_column(server_default=false())

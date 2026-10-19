from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from keystone.core.database import Base


class DictionaryItem(Base):
    __tablename__ = "dictionary_items"
    __table_args__ = (UniqueConstraint("dictionary_id", "value"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    dictionary_id: Mapped[UUID] = mapped_column(ForeignKey("keystone.dictionaries.id"))
    value: Mapped[str] = mapped_column(String(64))
    label: Mapped[str] = mapped_column(String(128))
    sort_order: Mapped[int] = mapped_column(default=0)
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_by: Mapped[UUID | None]
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_by: Mapped[UUID | None]

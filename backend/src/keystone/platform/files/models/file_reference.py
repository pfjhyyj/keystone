from uuid import UUID

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from keystone.core.database import Base


class FileReference(Base):
    __tablename__ = "file_references"
    owner: Mapped[str] = mapped_column(String(160), primary_key=True)
    slot: Mapped[str] = mapped_column(String(100), primary_key=True)
    file_id: Mapped[UUID] = mapped_column(
        ForeignKey("keystone.files.id"), primary_key=True, index=True
    )

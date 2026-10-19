from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from keystone.core.database import Base


class File(Base):
    __tablename__ = "files"
    __table_args__ = (
        CheckConstraint(
            "status IN ('uploading','active','disabled','failed','deleting','deleted')",
            name="file_status",
        ),
        CheckConstraint("(status = 'deleted') = (deleted_at IS NOT NULL)", name="file_deleted_at"),
        CheckConstraint("size_bytes IS NULL OR size_bytes >= 0", name="file_size"),
        CheckConstraint(
            "status NOT IN ('active','disabled') OR "
            "(size_bytes IS NOT NULL AND checksum_sha256 IS NOT NULL)",
            name="file_complete",
        ),
        Index("ix_files_status_updated", "status", "updated_at"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(255))
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("keystone.users.id"), index=True)
    content_type: Mapped[str] = mapped_column(String(128))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    checksum_sha256: Mapped[str | None] = mapped_column(String(64))
    upload_key: Mapped[str] = mapped_column(String(64), unique=True)
    upload_digest: Mapped[str] = mapped_column(String(64))
    storage_key: Mapped[str] = mapped_column(String(64))
    storage_fingerprint: Mapped[str] = mapped_column(String(64))
    object_key: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(16))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_by: Mapped[UUID | None]
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_by: Mapped[UUID | None]

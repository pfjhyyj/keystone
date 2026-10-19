from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from keystone.core.database import Base


class FileGrant(Base):
    __tablename__ = "file_grants"
    __table_args__ = (
        CheckConstraint(
            "(subject_type = 'user' AND user_id IS NOT NULL AND role_id IS NULL) OR "
            "(subject_type = 'role' AND role_id IS NOT NULL AND user_id IS NULL) OR "
            "(subject_type = 'authenticated' AND user_id IS NULL AND role_id IS NULL)",
            name="file_grant_subject",
        ),
        CheckConstraint(
            "action IN ('inspect','content_read','manage') AND "
            "NOT (subject_type = 'authenticated' AND action = 'manage')",
            name="file_grant_action",
        ),
        Index(
            "uq_file_grant_user",
            "file_id",
            "user_id",
            "action",
            unique=True,
            postgresql_where=text("subject_type = 'user'"),
        ),
        Index(
            "uq_file_grant_role",
            "file_id",
            "role_id",
            "action",
            unique=True,
            postgresql_where=text("subject_type = 'role'"),
        ),
        Index(
            "uq_file_grant_authenticated",
            "file_id",
            "action",
            unique=True,
            postgresql_where=text("subject_type = 'authenticated'"),
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    file_id: Mapped[UUID] = mapped_column(ForeignKey("keystone.files.id"), index=True)
    subject_type: Mapped[str] = mapped_column(String(16))
    user_id: Mapped[UUID | None] = mapped_column(ForeignKey("keystone.users.id"))
    role_id: Mapped[UUID | None] = mapped_column(ForeignKey("keystone.roles.id"), index=True)
    action: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_by: Mapped[UUID | None]

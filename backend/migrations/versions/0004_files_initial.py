"""Initialize the files domain.

Tables: files, file_references.
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "files",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("content_type", sa.String(length=128), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("checksum_sha256", sa.String(length=64), nullable=True),
        sa.Column("upload_key", sa.String(length=64), nullable=False),
        sa.Column("upload_digest", sa.String(length=64), nullable=False),
        sa.Column("storage_key", sa.String(length=64), nullable=False),
        sa.Column("storage_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("object_key", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            "(status = 'deleted') = (deleted_at IS NOT NULL)", name="file_deleted_at"
        ),
        sa.CheckConstraint(
            "status IN ('uploading','active','disabled','failed','deleting','deleted')",
            name="file_status",
        ),
        sa.CheckConstraint(
            "status NOT IN ('active','disabled') OR "
            "(size_bytes IS NOT NULL AND checksum_sha256 IS NOT NULL)",
            name="file_complete",
        ),
        sa.CheckConstraint("size_bytes IS NULL OR size_bytes >= 0", name="file_size"),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["keystone.users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("object_key"),
        sa.UniqueConstraint("upload_key"),
        schema="keystone",
    )
    op.create_index(
        "ix_files_status_updated",
        "files",
        ["status", "updated_at"],
        unique=False,
        schema="keystone",
    )
    op.create_index(
        op.f("ix_keystone_files_owner_id"), "files", ["owner_id"], unique=False, schema="keystone"
    )
    op.create_index(
        "ix_files_status_completed", "files", ["status", "completed_at"], schema="keystone"
    )
    op.create_table(
        "file_references",
        sa.Column("owner", sa.String(length=160), nullable=False),
        sa.Column("slot", sa.String(length=100), nullable=False),
        sa.Column("file_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["file_id"],
            ["keystone.files.id"],
        ),
        sa.PrimaryKeyConstraint("owner", "slot", "file_id"),
        schema="keystone",
    )
    op.create_index(
        op.f("ix_keystone_file_references_file_id"),
        "file_references",
        ["file_id"],
        unique=False,
        schema="keystone",
    )


def downgrade():
    op.drop_table("file_references", schema="keystone")
    op.drop_table("files", schema="keystone")

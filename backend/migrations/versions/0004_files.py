"""Initialize the files domain.

Tables: files, file_grants, file_references.
"""

import sqlalchemy as sa
from alembic import op

revision = "0004_files"
down_revision = "0003_dictionary"
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
    op.create_table(
        "file_grants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("file_id", sa.Uuid(), nullable=False),
        sa.Column("subject_type", sa.String(length=16), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("role_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            "(subject_type = 'user' AND user_id IS NOT NULL AND role_id IS NULL) OR "
            "(subject_type = 'role' AND role_id IS NOT NULL AND user_id IS NULL) OR "
            "(subject_type = 'authenticated' AND user_id IS NULL AND role_id IS NULL)",
            name="file_grant_subject",
        ),
        sa.CheckConstraint(
            "action IN ('inspect','content_read','manage') AND "
            "NOT (subject_type = 'authenticated' AND action = 'manage')",
            name="file_grant_action",
        ),
        sa.ForeignKeyConstraint(
            ["file_id"],
            ["keystone.files.id"],
        ),
        sa.ForeignKeyConstraint(
            ["role_id"],
            ["keystone.roles.id"],
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["keystone.users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        schema="keystone",
    )
    op.create_index(
        op.f("ix_keystone_file_grants_file_id"),
        "file_grants",
        ["file_id"],
        unique=False,
        schema="keystone",
    )
    op.create_index(
        op.f("ix_keystone_file_grants_role_id"),
        "file_grants",
        ["role_id"],
        unique=False,
        schema="keystone",
    )
    op.create_index(
        "uq_file_grant_authenticated",
        "file_grants",
        ["file_id", "action"],
        unique=True,
        schema="keystone",
        postgresql_where=sa.text("subject_type = 'authenticated'"),
    )
    op.create_index(
        "uq_file_grant_role",
        "file_grants",
        ["file_id", "role_id", "action"],
        unique=True,
        schema="keystone",
        postgresql_where=sa.text("subject_type = 'role'"),
    )
    op.create_index(
        "uq_file_grant_user",
        "file_grants",
        ["file_id", "user_id", "action"],
        unique=True,
        schema="keystone",
        postgresql_where=sa.text("subject_type = 'user'"),
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
    op.drop_table("file_grants", schema="keystone")
    op.drop_table("files", schema="keystone")

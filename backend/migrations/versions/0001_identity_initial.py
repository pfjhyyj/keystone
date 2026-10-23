"""Initialize the identity domain.

Tables: users, roles, user_roles, role_permissions, login_sessions, personal_access_tokens.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("password_hash", sa.String(length=256), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("password_change_required", sa.Boolean(), nullable=False),
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username"),
        schema="keystone",
    )
    op.create_table(
        "roles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.String(length=1000), nullable=False),
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key"),
        schema="keystone",
    )
    op.create_table(
        "user_roles",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["role_id"],
            ["keystone.roles.id"],
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["keystone.users.id"],
        ),
        sa.PrimaryKeyConstraint("user_id", "role_id"),
        schema="keystone",
    )
    op.create_index(
        op.f("ix_keystone_user_roles_role_id"),
        "user_roles",
        ["role_id"],
        unique=False,
        schema="keystone",
    )
    op.create_table(
        "role_permissions",
        sa.Column("role_id", sa.Uuid(), nullable=False),
        sa.Column("permission_key", sa.String(length=100), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(["role_id"], ["keystone.roles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("role_id", "permission_key"),
        schema="keystone",
    )
    op.create_table(
        "login_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_digest", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["keystone.users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_digest"),
        schema="keystone",
    )
    op.create_index(
        op.f("ix_keystone_login_sessions_user_id"),
        "login_sessions",
        ["user_id"],
        unique=False,
        schema="keystone",
    )
    op.create_table(
        "personal_access_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("token_digest", sa.String(length=64), nullable=False),
        sa.Column("permissions", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["keystone.users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_digest"),
        schema="keystone",
    )
    op.create_index(
        op.f("ix_keystone_personal_access_tokens_user_id"),
        "personal_access_tokens",
        ["user_id"],
        unique=False,
        schema="keystone",
    )


def downgrade():
    op.drop_table("personal_access_tokens", schema="keystone")
    op.drop_table("login_sessions", schema="keystone")
    op.drop_table("role_permissions", schema="keystone")
    op.drop_table("user_roles", schema="keystone")
    op.drop_table("roles", schema="keystone")
    op.drop_table("users", schema="keystone")

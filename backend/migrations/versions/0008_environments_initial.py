"""Interactive environments and sessions.

Revision ID: 0008
Revises: 0007
"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "environments",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("owner_id", sa.Uuid(), sa.ForeignKey("keystone.users.id"), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("volume", sa.String(128), unique=True, nullable=False),
        sa.Column(
            "state_initialization_pending",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("backup_at", sa.DateTime(timezone=True)),
        sa.Column("backup_failed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.CheckConstraint("kind IN ('chromium')", name="ck_environment_kind"),
        sa.UniqueConstraint("owner_id", "kind", "key", name="uq_environment_owner_kind_key"),
        schema="keystone",
    )
    op.create_index(
        "ix_keystone_environments_owner_id",
        "environments",
        ["owner_id"],
        schema="keystone",
    )
    op.create_table(
        "environment_sessions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "environment_id",
            sa.Uuid(),
            sa.ForeignKey("keystone.environments.id"),
            nullable=False,
        ),
        sa.Column("purpose", sa.String(16), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("sandbox_id", sa.String(128), unique=True),
        sa.Column("stop_requested", sa.Boolean(), nullable=False),
        sa.Column("delete_sent", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "last_activity_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("operation_until", sa.DateTime(timezone=True)),
        sa.Column("control_owner", sa.Uuid()),
        sa.Column("control_kind", sa.String(16)),
        sa.Column("control_until", sa.DateTime(timezone=True)),
        sa.Column(
            "control_generation", sa.BigInteger(), server_default=sa.text("0"), nullable=False
        ),
        sa.CheckConstraint(
            "state IN ('starting', 'ready', 'stopping', 'review', 'stopped', 'failed')",
            name="ck_environment_session_state",
        ),
        sa.CheckConstraint(
            "purpose IN ('interactive', 'backup')",
            name="ck_environment_session_purpose",
        ),
        sa.CheckConstraint(
            "(control_owner IS NULL) = (control_kind IS NULL)"
            " AND (control_owner IS NULL) = (control_until IS NULL)"
            " AND (control_kind IS NULL OR control_kind IN ('human', 'automation'))",
            name="ck_environment_session_control",
        ),
        schema="keystone",
    )
    op.create_index(
        "uq_environment_session_occupied",
        "environment_sessions",
        ["environment_id"],
        schema="keystone",
        unique=True,
        postgresql_where=sa.text("state IN ('starting', 'ready', 'stopping', 'review')"),
    )
    op.create_index(
        "ix_environment_session_history",
        "environment_sessions",
        ["environment_id", "created_at", "id"],
        schema="keystone",
    )


def downgrade():
    op.drop_table("environment_sessions", schema="keystone")
    op.drop_table("environments", schema="keystone")

"""Browser workspaces and runs.

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
        "browser_workspaces",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("owner_id", sa.Uuid(), sa.ForeignKey("keystone.users.id"), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("volume", sa.String(128), unique=True, nullable=False),
        sa.Column("backup_at", sa.DateTime(timezone=True)),
        sa.Column("backup_failed", sa.Boolean(), nullable=False, server_default=sa.false()),
        schema="keystone",
    )
    op.create_index(
        "ix_keystone_browser_workspaces_owner_id",
        "browser_workspaces",
        ["owner_id"],
        schema="keystone",
    )
    op.create_table(
        "browser_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.Uuid(),
            sa.ForeignKey("keystone.browser_workspaces.id"),
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
        sa.CheckConstraint(
            "state IN ('starting', 'ready', 'stopping', 'review', 'stopped', 'failed')",
            name="ck_browser_run_state",
        ),
        sa.CheckConstraint(
            "purpose IN ('browse', 'backup')",
            name="ck_browser_run_purpose",
        ),
        schema="keystone",
    )
    op.create_index(
        "uq_browser_run_occupied",
        "browser_runs",
        ["workspace_id"],
        schema="keystone",
        unique=True,
        postgresql_where=sa.text("state IN ('starting', 'ready', 'stopping', 'review')"),
    )
    op.create_index(
        "ix_browser_run_history",
        "browser_runs",
        ["workspace_id", "created_at", "id"],
        schema="keystone",
    )


def downgrade():
    op.drop_table("browser_runs", schema="keystone")
    op.drop_table("browser_workspaces", schema="keystone")

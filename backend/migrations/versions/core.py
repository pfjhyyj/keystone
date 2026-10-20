"""Initialize core runtime observations: always-on Worker heartbeats."""

import sqlalchemy as sa
from alembic import op

revision = "core"
down_revision = "flow"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "worker_heartbeats",
        sa.Column("instance_id", sa.Uuid(), nullable=False),
        sa.Column("host_id", sa.String(255), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state IN ('starting', 'ready', 'unready', 'stopped')", name="worker_heartbeat_state"
        ),
        sa.PrimaryKeyConstraint("instance_id"),
        schema="keystone",
    )
    op.create_index(
        "ix_keystone_worker_heartbeats_heartbeat_at",
        "worker_heartbeats",
        ["heartbeat_at"],
        schema="keystone",
    )


def downgrade():
    op.drop_table("worker_heartbeats", schema="keystone")

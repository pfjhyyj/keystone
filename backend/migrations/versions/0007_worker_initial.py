"""Initialize the worker domain.

Tables: worker_registrations, worker_heartbeats.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "worker_registrations",
        sa.Column("worker_id", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=16), server_default="registered", nullable=False),
        sa.Column("host_id", sa.String(length=255), nullable=True),
        sa.Column("coordination_id", sa.String(length=64), nullable=True),
        sa.Column("execution_version", sa.String(length=64), nullable=True),
        sa.Column("config_digest", sa.String(length=64), nullable=True),
        sa.Column("instance_id", sa.Uuid(), nullable=True),
        sa.Column(
            "registered_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("state IN ('registered','retired')", name="worker_registration_state"),
        sa.CheckConstraint("(state = 'retired') = (retired_at IS NOT NULL)", name="worker_retired"),
        sa.PrimaryKeyConstraint("worker_id"),
        schema="keystone",
    )
    op.create_table(
        "worker_heartbeats",
        sa.Column("instance_id", sa.Uuid(), nullable=False),
        sa.Column("host_id", sa.String(length=255), nullable=False),
        sa.Column(
            "worker_id",
            sa.String(length=64),
            server_default="keystone-worker",
            nullable=False,
        ),
        sa.Column("execution_version", sa.String(length=64), nullable=True),
        sa.Column("config_digest", sa.String(length=64), nullable=True),
        sa.Column("coordination_id", sa.String(length=64), nullable=True),
        sa.Column(
            "task_results",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state IN ('starting','ready','unready','draining','stopped')",
            name="worker_heartbeat_state",
        ),
        sa.PrimaryKeyConstraint("instance_id"),
        schema="keystone",
    )
    op.create_index(
        "ix_keystone_worker_heartbeats_heartbeat_at",
        "worker_heartbeats",
        ["heartbeat_at"],
        unique=False,
        schema="keystone",
    )
    op.create_index(
        "ix_worker_identity_start",
        "worker_heartbeats",
        ["worker_id", "started_at"],
        unique=False,
        schema="keystone",
    )


def downgrade():
    op.drop_table("worker_heartbeats", schema="keystone")
    op.drop_table("worker_registrations", schema="keystone")

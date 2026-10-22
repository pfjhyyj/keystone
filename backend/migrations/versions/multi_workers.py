"""Fixed Worker ownership, shared health and fair Flow reconciliation."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "multi_workers"
down_revision = "model_costs"
branch_labels = None
depends_on = None


def upgrade():
    for column in (
        sa.Column("worker_id", sa.String(64)),
        sa.Column("worker_instance_id", UUID()),
        sa.Column("execution_version", sa.String(64)),
    ):
        op.add_column("model_invocations", column, schema="keystone")
    op.create_check_constraint(
        "invocation_worker_owner",
        "model_invocations",
        "(worker_id IS NULL AND worker_instance_id IS NULL AND execution_version IS NULL) OR "
        "(worker_id IS NOT NULL AND worker_instance_id IS NOT NULL "
        "AND execution_version IS NOT NULL)",
        schema="keystone",
    )
    op.create_index(
        "ix_invocation_worker_running",
        "model_invocations",
        ["worker_id", "status"],
        schema="keystone",
    )
    op.add_column(
        "flow_runs",
        sa.Column("reconcile_attempted_at", sa.DateTime(timezone=True)),
        schema="keystone",
    )
    op.create_index(
        "ix_flow_reconcile_attempt",
        "flow_runs",
        ["status", "reconcile_attempted_at", "id"],
        schema="keystone",
    )
    for column in (
        sa.Column("worker_id", sa.String(64), nullable=False, server_default="keystone-worker"),
        sa.Column("execution_version", sa.String(64)),
        sa.Column("config_digest", sa.String(64)),
        sa.Column("coordination_id", sa.String(64)),
        sa.Column("task_results", JSONB(), nullable=False, server_default="{}"),
    ):
        op.add_column("worker_heartbeats", column, schema="keystone")
    op.drop_constraint("worker_heartbeat_state", "worker_heartbeats", schema="keystone")
    op.create_check_constraint(
        "worker_heartbeat_state",
        "worker_heartbeats",
        "state IN ('starting','ready','unready','draining','stopped')",
        schema="keystone",
    )
    op.create_index(
        "ix_worker_identity_start",
        "worker_heartbeats",
        ["worker_id", "started_at"],
        schema="keystone",
    )


def downgrade():
    op.execute("UPDATE keystone.worker_heartbeats SET state='unready' WHERE state='draining'")
    op.drop_index("ix_worker_identity_start", table_name="worker_heartbeats", schema="keystone")
    op.drop_constraint("worker_heartbeat_state", "worker_heartbeats", schema="keystone")
    op.create_check_constraint(
        "worker_heartbeat_state",
        "worker_heartbeats",
        "state IN ('starting','ready','unready','stopped')",
        schema="keystone",
    )
    for name in (
        "worker_id",
        "execution_version",
        "config_digest",
        "coordination_id",
        "task_results",
    ):
        op.drop_column("worker_heartbeats", name, schema="keystone")
    op.drop_index("ix_flow_reconcile_attempt", table_name="flow_runs", schema="keystone")
    op.drop_column("flow_runs", "reconcile_attempted_at", schema="keystone")
    op.drop_index("ix_invocation_worker_running", table_name="model_invocations", schema="keystone")
    op.drop_constraint("invocation_worker_owner", "model_invocations", schema="keystone")
    for name in ("worker_id", "worker_instance_id", "execution_version"):
        op.drop_column("model_invocations", name, schema="keystone")

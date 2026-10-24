"""Initialize the flow domain.

Tables: flow_runs, flow_run_budgets, flow_budget_claims.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def nonnegative(column, fields):
    required = "ARRAY[" + ",".join("'" + key + "'" for key in fields) + "]"
    return (
        f"jsonb_typeof({column}) = 'object' AND octet_length({column}::text) <= 4096 "
        f"AND {column} ?& {required} AND {column} - {required} = '{{}}'::jsonb AND "
        + " AND ".join(
            f"(jsonb_typeof({column}->'{key}') = 'number' AND ({column}->>'{key}')::numeric >= 0)"
            for key in fields
        )
    )


def upgrade():
    op.create_table(
        "flow_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("definition_key", sa.String(length=100), nullable=False),
        sa.Column("definition_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("execution_version", sa.String(length=64), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("personal_access_token_id", sa.Uuid(), nullable=True),
        sa.Column("request_key", sa.String(length=128), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("input", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "result", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True
        ),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reconcile_attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("(status = 'succeeded') = (result IS NOT NULL)", name="flow_result"),
        sa.CheckConstraint(
            "(status IN ('succeeded','failed','cancelled')) = (finished_at IS NOT NULL)",
            name="flow_finished",
        ),
        sa.CheckConstraint(
            "status IN ('pending','running','succeeded','failed','cancelled')", name="flow_status"
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["keystone.users.id"],
        ),
        sa.ForeignKeyConstraint(
            ["personal_access_token_id"],
            ["keystone.personal_access_tokens.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("actor_id", "request_key", name="flow_request_key"),
        schema="keystone",
    )
    op.create_index(
        "ix_flow_owner",
        "flow_runs",
        ["actor_id", "created_at", "id"],
        unique=False,
        schema="keystone",
    )
    op.create_index(
        "ix_flow_reconcile_attempt",
        "flow_runs",
        ["status", "reconcile_attempted_at", "id"],
        unique=False,
        schema="keystone",
    )
    op.create_index(
        "ix_flow_scan",
        "flow_runs",
        ["status", "observed_at", "id"],
        unique=False,
        schema="keystone",
    )

    counters = (
        "model_requests",
        "tool_calls",
        "iterations",
        "file_count",
        "file_bytes",
        "input_tokens",
        "output_tokens",
        "unreported_text_requests",
    )
    limits = (
        "model_requests",
        "tool_calls",
        "iterations",
        "file_count",
        "file_bytes",
        "input_tokens",
        "output_tokens",
        "concurrent_operations",
        "temporary_bytes",
    )
    op.create_table(
        "flow_run_budgets",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("limits", postgresql.JSONB(), nullable=False),
        sa.Column("counters", postgresql.JSONB(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(nonnegative("limits", limits), name="flow_budget_limits"),
        sa.CheckConstraint(nonnegative("counters", counters), name="flow_budget_counters"),
        sa.ForeignKeyConstraint(["run_id"], ["keystone.flow_runs.id"]),
        sa.PrimaryKeyConstraint("run_id"),
        schema="keystone",
    )
    op.create_table(
        "flow_budget_claims",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("amounts", postgresql.JSONB(), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "kind IN ('model','tokens','tool','iteration','file')", name="flow_claim_kind"
        ),
        sa.CheckConstraint(
            "key ~ '^[0-9a-f]{64}$' AND request_digest ~ '^[0-9a-f]{64}$'",
            name="flow_claim_identity",
        ),
        sa.CheckConstraint(nonnegative("amounts", counters), name="flow_claim_amounts"),
        sa.ForeignKeyConstraint(["run_id"], ["keystone.flow_run_budgets.run_id"]),
        sa.PrimaryKeyConstraint("run_id", "kind", "key"),
        schema="keystone",
    )


def downgrade():
    op.drop_table("flow_budget_claims", schema="keystone")
    op.drop_table("flow_run_budgets", schema="keystone")
    op.drop_table("flow_runs", schema="keystone")

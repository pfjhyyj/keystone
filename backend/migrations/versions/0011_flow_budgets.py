"""Run cumulative limits and stable reservations; historical runs are not backfilled."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0011"
down_revision = "0010"
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

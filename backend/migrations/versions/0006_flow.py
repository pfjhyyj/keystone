"""Initialize the flow domain.

Tables: flow_runs.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0006_flow"
down_revision = "0005_model_access"
branch_labels = None
depends_on = None


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
        "ix_flow_scan",
        "flow_runs",
        ["status", "observed_at", "id"],
        unique=False,
        schema="keystone",
    )


def downgrade():
    op.drop_table("flow_runs", schema="keystone")

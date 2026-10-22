"""Persist original billing scopes, bounded reconciliation and log pagination."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "model_costs"
down_revision = "core"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("channel_provider", "model_channels", schema="keystone")
    op.create_check_constraint(
        "channel_provider",
        "model_channels",
        "provider IN ('deepseek','openrouter','aliyun_bailian','openlux','yunwu')",
        schema="keystone",
    )
    for column in (
        sa.Column("cost_scope", sa.String(64)),
        sa.Column("cost_context", JSONB()),
        sa.Column("cost_checkpoint", JSONB()),
        sa.Column("cost_block_reason", sa.String(64)),
        sa.Column("cost_next_allowed_at", sa.DateTime(timezone=True)),
    ):
        op.add_column("model_channels", column, schema="keystone")
    op.create_index(
        "ix_keystone_model_channels_cost_scope", "model_channels", ["cost_scope"], schema="keystone"
    )
    for column in (
        sa.Column("cost_check_status", sa.String(16), nullable=False, server_default="unavailable"),
        sa.Column("cost_check_reason", sa.String(64)),
        sa.Column("cost_check_rounds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_check_errors", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_checked_at", sa.DateTime(timezone=True)),
        sa.Column("cost_next_check_at", sa.DateTime(timezone=True)),
        sa.Column("cost_deadline_at", sa.DateTime(timezone=True)),
        sa.Column("cost_context", JSONB()),
        sa.Column("receipt_ids", JSONB()),
        sa.Column("cost_evidence", JSONB()),
    ):
        op.add_column("model_invocations", column, schema="keystone")
    # No historical scope can be inferred from today's credentials.
    op.execute(
        "UPDATE keystone.model_invocations SET cost_check_status = "
        "CASE WHEN cost_amount IS NOT NULL THEN 'confirmed' ELSE 'unavailable' END, "
        "cost_check_reason = CASE WHEN cost_amount IS NULL "
        "THEN 'legacy_scope_unknown' ELSE NULL END"
    )
    op.create_check_constraint(
        "invocation_cost_check_status",
        "model_invocations",
        "cost_check_status IN ('pending','confirmed','blocked','unavailable','exhausted')",
        schema="keystone",
    )
    op.create_check_constraint(
        "invocation_cost_counts",
        "model_invocations",
        "cost_check_rounds >= 0 AND cost_check_errors >= 0",
        schema="keystone",
    )
    op.create_index(
        "ix_model_invocations_cost_due",
        "model_invocations",
        ["cost_check_status", "cost_next_check_at", "id"],
        schema="keystone",
    )


def downgrade():
    # Do not reinterpret new providers as old ones or discard their business records.
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM keystone.model_channels "
        "WHERE provider IN ('openlux','yunwu')) THEN RAISE EXCEPTION "
        "'Remove new-provider data in an isolated restore before downgrade'; "
        "END IF; END $$"
    )
    op.drop_index(
        "ix_model_invocations_cost_due", table_name="model_invocations", schema="keystone"
    )
    for name in ("invocation_cost_check_status", "invocation_cost_counts"):
        op.drop_constraint(name, "model_invocations", schema="keystone")
    for name in (
        "cost_check_status",
        "cost_check_reason",
        "cost_check_rounds",
        "cost_check_errors",
        "cost_checked_at",
        "cost_next_check_at",
        "cost_deadline_at",
        "cost_context",
        "receipt_ids",
        "cost_evidence",
    ):
        op.drop_column("model_invocations", name, schema="keystone")
    op.drop_index(
        "ix_keystone_model_channels_cost_scope", table_name="model_channels", schema="keystone"
    )
    for name in (
        "cost_scope",
        "cost_context",
        "cost_checkpoint",
        "cost_block_reason",
        "cost_next_allowed_at",
    ):
        op.drop_column("model_channels", name, schema="keystone")
    op.drop_constraint("channel_provider", "model_channels", schema="keystone")
    op.create_check_constraint(
        "channel_provider",
        "model_channels",
        "provider IN ('deepseek','openrouter','aliyun_bailian')",
        schema="keystone",
    )

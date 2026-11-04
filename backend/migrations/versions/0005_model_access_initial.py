"""Initialize the model_access domain.

Tables: model_channels, model_entries, model_invocations, model_dispatch_scopes.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "model_channels",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("api_base", sa.String(length=512), nullable=False),
        sa.Column("api_key_encrypted", sa.Text(), nullable=True),
        sa.Column("billing_api_key_encrypted", sa.Text(), nullable=True),
        sa.Column("workspace_id", sa.String(63), nullable=True),
        sa.Column("upstream_account_ref", sa.String(length=128), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("balance_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("balance_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("balance_last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("balance_check_status", sa.String(length=16), nullable=False),
        sa.Column("cost_scope", sa.String(length=64), nullable=True),
        sa.Column("cost_context", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("cost_checkpoint", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("cost_block_reason", sa.String(length=64), nullable=True),
        sa.Column("cost_next_allowed_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint(
            "balance_check_status IN ('never','ok','error','unavailable')",
            name="channel_balance_status",
        ),
        sa.CheckConstraint(
            "provider IN ('deepseek','openrouter','aliyun_bailian',"
            "'newapi','openlux','yunwu','volcengine')",
            name="channel_provider",
        ),
        sa.CheckConstraint(
            "workspace_id IS NULL OR (provider = 'aliyun_bailian' "
            "AND workspace_id ~ '^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$')",
            name="channel_workspace",
        ),
        sa.PrimaryKeyConstraint("id"),
        schema="keystone",
    )
    op.create_index(
        "ix_keystone_model_channels_cost_scope",
        "model_channels",
        ["cost_scope"],
        unique=False,
        schema="keystone",
    )
    op.create_table(
        "model_entries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), server_default="", nullable=False),
        sa.Column("channel_id", sa.Uuid(), nullable=False),
        sa.Column("provider_model", sa.String(length=128), nullable=False),
        sa.Column(
            "operations",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[\"text.generate\"]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "pricing",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text('\'{"mode":"inherit","rules":[]}\'::jsonb'),
        ),
        sa.Column("pricing_revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False),
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
        sa.CheckConstraint(
            "jsonb_typeof(operations) = 'array' AND operations <> '[]'::jsonb",
            name="model_entry_operations",
        ),
        sa.CheckConstraint("pricing_revision >= 0", name="model_entry_pricing_revision"),
        sa.CheckConstraint(
            "jsonb_typeof(pricing) = 'object' AND octet_length(pricing::text) <= 262144",
            name="model_entry_pricing",
        ),
        sa.ForeignKeyConstraint(
            ["channel_id"],
            ["keystone.model_channels.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key"),
        schema="keystone",
    )
    op.create_index(
        op.f("ix_keystone_model_entries_channel_id"),
        "model_entries",
        ["channel_id"],
        unique=False,
        schema="keystone",
    )
    op.create_table(
        "model_invocations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("worker_id", sa.String(length=64), nullable=True),
        sa.Column("worker_instance_id", sa.Uuid(), nullable=True),
        sa.Column("execution_version", sa.String(length=64), nullable=True),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("flow_context", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("model_entry_id", sa.Uuid(), nullable=False),
        sa.Column("channel_id", sa.Uuid(), nullable=False),
        sa.Column("provider_model", sa.String(length=128), nullable=False),
        sa.Column("upstream_request_id", sa.String(length=200), nullable=True),
        sa.Column(
            "operation",
            sa.String(32),
            nullable=False,
            server_default="text.generate",
        ),
        sa.Column(
            "result_context",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("usage", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("cost_amount", sa.Numeric(precision=38, scale=18), nullable=True),
        sa.Column("cost_unit", sa.String(length=32), nullable=True),
        sa.Column("cost_source", sa.String(length=16), nullable=False),
        sa.Column("estimate_snapshot", postgresql.JSONB()),
        sa.Column("estimate_facts", postgresql.JSONB()),
        sa.Column("estimate_status", sa.String(16), nullable=False, server_default="unavailable"),
        sa.Column("estimate_amount", sa.Numeric(38, 18)),
        sa.Column("estimate_currency", sa.String(3)),
        sa.Column("estimate_reason", sa.String(64), server_default="snapshot_missing"),
        sa.Column("estimate_detail", postgresql.JSONB()),
        sa.Column("estimate_checked_at", sa.DateTime(timezone=True)),
        sa.Column("cost_note", sa.String(length=100), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column(
            "cost_check_status",
            sa.String(length=16),
            server_default="unavailable",
            nullable=False,
        ),
        sa.Column("cost_check_reason", sa.String(length=64), nullable=True),
        sa.Column("cost_check_rounds", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cost_check_errors", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cost_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cost_next_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cost_deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cost_context", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("receipt_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("cost_evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "result_context IS NULL OR (jsonb_typeof(result_context) = 'object' "
            "AND octet_length(result_context::text) <= 16384)",
            name="invocation_result_context",
        ),
        sa.CheckConstraint(
            "(cost_amount IS NULL AND cost_unit IS NULL AND cost_source = 'unknown') OR "
            "(cost_amount IS NOT NULL AND cost_amount >= 0 AND cost_unit IS NOT NULL "
            "AND cost_source IN ('response','query'))",
            name="invocation_cost",
        ),
        sa.CheckConstraint(
            "cost_check_status IN ('pending','confirmed','blocked','unavailable','exhausted')",
            name="invocation_cost_check_status",
        ),
        sa.CheckConstraint(
            "cost_check_rounds >= 0 AND cost_check_errors >= 0", name="invocation_cost_counts"
        ),
        sa.CheckConstraint(
            "(status = 'running') = (finished_at IS NULL)", name="invocation_finished"
        ),
        sa.CheckConstraint(
            "status IN ('running','succeeded','failed','unknown')", name="invocation_status"
        ),
        sa.CheckConstraint(
            "(worker_id IS NULL AND worker_instance_id IS NULL AND execution_version IS NULL) OR "
            "(worker_id IS NOT NULL AND worker_instance_id IS NOT NULL "
            "AND execution_version IS NOT NULL)",
            name="invocation_worker_owner",
        ),
        sa.CheckConstraint(
            "estimate_status IN ('pending','estimated','partial','unavailable','invalid')",
            name="invocation_estimate_status",
        ),
        sa.CheckConstraint(
            "(estimate_status = 'estimated' AND estimate_amount IS NOT NULL "
            "AND estimate_amount >= 0 AND estimate_amount < 1e20 "
            "AND estimate_currency IS NOT NULL) OR "
            "(estimate_status <> 'estimated' AND estimate_amount IS NULL)",
            name="invocation_estimate_amount",
        ),
        sa.CheckConstraint(
            "estimate_snapshot IS NULL OR (jsonb_typeof(estimate_snapshot) = 'object' "
            "AND octet_length(estimate_snapshot::text) <= 24576)",
            name="invocation_estimate_snapshot",
        ),
        sa.CheckConstraint(
            "estimate_facts IS NULL OR (jsonb_typeof(estimate_facts) = 'object' "
            "AND octet_length(estimate_facts::text) <= 8192)",
            name="invocation_estimate_facts",
        ),
        sa.CheckConstraint(
            "estimate_detail IS NULL OR (jsonb_typeof(estimate_detail) = 'object' "
            "AND octet_length(estimate_detail::text) <= 16384)",
            name="invocation_estimate_detail",
        ),
        sa.ForeignKeyConstraint(
            ["channel_id"],
            ["keystone.model_channels.id"],
        ),
        sa.ForeignKeyConstraint(
            ["model_entry_id"],
            ["keystone.model_entries.id"],
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["keystone.users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        schema="keystone",
    )
    op.create_index(
        "ix_model_invocations_cost_due",
        "model_invocations",
        ["cost_check_status", "cost_next_check_at", "id"],
        unique=False,
        schema="keystone",
    )
    op.create_index(
        "ix_invocation_worker_running",
        "model_invocations",
        ["worker_id", "status"],
        unique=False,
        schema="keystone",
    )
    op.create_index(
        "ix_model_invocations_time",
        "model_invocations",
        ["created_at", "id"],
        unique=False,
        schema="keystone",
    )
    op.create_index(
        "ix_model_invocations_user_time",
        "model_invocations",
        ["user_id", "created_at", "id"],
        unique=False,
        schema="keystone",
    )

    op.create_index(
        "ix_model_invocations_flow_time",
        "model_invocations",
        [sa.text("(flow_context ->> 'flow_run_id')"), "created_at", "id"],
        schema="keystone",
    )
    op.create_table(
        "model_dispatch_scopes",
        sa.Column("scope", sa.String(64), nullable=False),
        sa.Column(
            "next_send_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "blocked_until",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("scope ~ '^[0-9a-f]{64}$'", name="dispatch_scope_identity"),
        sa.PrimaryKeyConstraint("scope"),
        schema="keystone",
    )


def downgrade():
    op.drop_table("model_dispatch_scopes", schema="keystone")
    op.drop_table("model_invocations", schema="keystone")
    op.drop_table("model_entries", schema="keystone")
    op.drop_table("model_channels", schema="keystone")

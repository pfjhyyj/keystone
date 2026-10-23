"""Initialize the model_access domain.

Tables: model_channels, model_entries, model_invocations.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "model_access"
down_revision = "files"
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
        sa.Column("upstream_account_ref", sa.String(length=128), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("balance_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("balance_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("balance_last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("balance_check_status", sa.String(length=16), nullable=False),
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
            "provider IN ('deepseek','openrouter','aliyun_bailian')", name="channel_provider"
        ),
        sa.PrimaryKeyConstraint("id"),
        schema="keystone",
    )
    op.create_table(
        "model_entries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("channel_id", sa.Uuid(), nullable=False),
        sa.Column("provider_model", sa.String(length=128), nullable=False),
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
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("flow_context", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("model_entry_id", sa.Uuid(), nullable=False),
        sa.Column("channel_id", sa.Uuid(), nullable=False),
        sa.Column("provider_model", sa.String(length=128), nullable=False),
        sa.Column("upstream_request_id", sa.String(length=200), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("usage", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("cost_amount", sa.Numeric(precision=38, scale=18), nullable=True),
        sa.Column("cost_unit", sa.String(length=32), nullable=True),
        sa.Column("cost_source", sa.String(length=16), nullable=False),
        sa.Column("cost_note", sa.String(length=100), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
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
            "(cost_amount IS NULL AND cost_unit IS NULL AND cost_source = 'unknown') OR "
            "(cost_amount IS NOT NULL AND cost_amount >= 0 AND cost_unit IS NOT NULL "
            "AND cost_source IN ('response','query'))",
            name="invocation_cost",
        ),
        sa.CheckConstraint(
            "(status = 'running') = (finished_at IS NULL)", name="invocation_finished"
        ),
        sa.CheckConstraint(
            "status IN ('running','succeeded','failed','unknown')", name="invocation_status"
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


def downgrade():
    op.drop_table("model_invocations", schema="keystone")
    op.drop_table("model_entries", schema="keystone")
    op.drop_table("model_channels", schema="keystone")

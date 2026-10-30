"""Immutable per-invocation estimated pricing, separate from supplier receipts."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "model_entries",
        sa.Column(
            "pricing",
            JSONB,
            nullable=False,
            server_default=sa.text('\'{"mode":"inherit","rules":[]}\'::jsonb'),
        ),
        schema="keystone",
    )
    op.add_column(
        "model_entries",
        sa.Column("pricing_revision", sa.Integer, nullable=False, server_default="0"),
        schema="keystone",
    )
    op.create_check_constraint(
        "model_entry_pricing_revision", "model_entries", "pricing_revision >= 0", schema="keystone"
    )
    op.create_check_constraint(
        "model_entry_pricing",
        "model_entries",
        "jsonb_typeof(pricing) = 'object' AND octet_length(pricing::text) <= 262144",
        schema="keystone",
    )
    for column in (
        sa.Column("estimate_snapshot", JSONB),
        sa.Column("estimate_facts", JSONB),
        sa.Column("estimate_status", sa.String(16), nullable=False, server_default="unavailable"),
        sa.Column("estimate_amount", sa.Numeric(38, 18)),
        sa.Column("estimate_currency", sa.String(3)),
        sa.Column("estimate_reason", sa.String(64), server_default="legacy_no_snapshot"),
        sa.Column("estimate_detail", JSONB),
        sa.Column("estimate_checked_at", sa.DateTime(timezone=True)),
    ):
        op.add_column("model_invocations", column, schema="keystone")
    op.create_check_constraint(
        "invocation_estimate_status",
        "model_invocations",
        "estimate_status IN ('pending','estimated','partial','unavailable','invalid')",
        schema="keystone",
    )
    op.create_check_constraint(
        "invocation_estimate_amount",
        "model_invocations",
        "(estimate_status = 'estimated' AND estimate_amount IS NOT NULL "
        "AND estimate_amount >= 0 AND estimate_amount < 1e20 AND estimate_currency IS NOT NULL) OR "
        "(estimate_status <> 'estimated' AND estimate_amount IS NULL)",
        schema="keystone",
    )
    for field, limit in (
        ("estimate_snapshot", 24576),
        ("estimate_facts", 8192),
        ("estimate_detail", 16384),
    ):
        op.create_check_constraint(
            f"invocation_{field}",
            "model_invocations",
            f"{field} IS NULL OR (jsonb_typeof({field}) = 'object' "
            f"AND octet_length({field}::text) <= {limit})",
            schema="keystone",
        )


def downgrade():
    op.drop_constraint(
        "model_entry_pricing_revision", "model_entries", type_="check", schema="keystone"
    )
    op.drop_constraint("model_entry_pricing", "model_entries", type_="check", schema="keystone")
    for name in (
        "invocation_estimate_status",
        "invocation_estimate_amount",
        "invocation_estimate_snapshot",
        "invocation_estimate_facts",
        "invocation_estimate_detail",
    ):
        op.drop_constraint(name, "model_invocations", type_="check", schema="keystone")
    for name in (
        "estimate_snapshot",
        "estimate_facts",
        "estimate_status",
        "estimate_amount",
        "estimate_currency",
        "estimate_reason",
        "estimate_detail",
        "estimate_checked_at",
    ):
        op.drop_column("model_invocations", name, schema="keystone")
    op.drop_column("model_entries", "pricing_revision", schema="keystone")
    op.drop_column("model_entries", "pricing", schema="keystone")

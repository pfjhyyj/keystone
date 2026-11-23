"""Messaging channels, destinations and deliveries.

Revision ID: 0009
Revises: 0008
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def _audited():
    return (
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("created_by", sa.Uuid()),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("updated_by", sa.Uuid()),
    )


def upgrade():
    op.create_table(
        "message_channels",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("credential_encrypted", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        *_audited(),
        sa.CheckConstraint("provider IN ('feishu_webhook')", name="ck_message_channel_provider"),
        schema="keystone",
    )
    op.create_table(
        "message_destinations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("key", sa.String(64), unique=True, nullable=False),
        sa.Column(
            "channel_id",
            sa.Uuid(),
            sa.ForeignKey("keystone.message_channels.id"),
            nullable=False,
        ),
        sa.Column("external_id", sa.String(128)),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        *_audited(),
        schema="keystone",
    )
    op.create_index(
        "ix_keystone_message_destinations_channel_id",
        "message_destinations",
        ["channel_id"],
        schema="keystone",
    )
    op.create_table(
        "message_deliveries",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "channel_id",
            sa.Uuid(),
            sa.ForeignKey("keystone.message_channels.id"),
            nullable=False,
        ),
        sa.Column(
            "destination_id",
            sa.Uuid(),
            sa.ForeignKey("keystone.message_destinations.id"),
            nullable=False,
        ),
        sa.Column("source_module", sa.String(48), nullable=False),
        sa.Column("source_type", sa.String(64), nullable=False),
        sa.Column("source_id", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("content", postgresql.JSONB(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("created_by", sa.Uuid()),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'cancelled', 'sending', 'succeeded', 'failed', 'unknown',"
            " 'expired')",
            name="ck_message_delivery_status",
        ),
        sa.CheckConstraint("kind IN ('text', 'card')", name="ck_message_delivery_kind"),
        schema="keystone",
    )
    op.create_index(
        "ix_message_delivery_open",
        "message_deliveries",
        ["status", "created_at"],
        schema="keystone",
        postgresql_where=sa.text("status IN ('pending', 'sending')"),
    )
    op.create_index(
        "ix_message_delivery_history",
        "message_deliveries",
        ["created_at", "id"],
        schema="keystone",
    )


def downgrade():
    op.drop_table("message_deliveries", schema="keystone")
    op.drop_table("message_destinations", schema="keystone")
    op.drop_table("message_channels", schema="keystone")

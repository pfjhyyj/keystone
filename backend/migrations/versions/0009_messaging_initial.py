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
        sa.Column("app_id", sa.String(64)),
        sa.Column("credential_encrypted", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        *_audited(),
        sa.CheckConstraint(
            "provider IN ('feishu_webhook', 'feishu_app')", name="ck_message_channel_provider"
        ),
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
        "message_external_accounts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "channel_id",
            sa.Uuid(),
            sa.ForeignKey("keystone.message_channels.id"),
            nullable=False,
        ),
        sa.Column("external_user_id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("created_by", sa.Uuid()),
        sa.UniqueConstraint("channel_id", "external_user_id", name="uq_message_account_external"),
        sa.UniqueConstraint("channel_id", "user_id", name="uq_message_account_user"),
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
        ),
        sa.Column("recipient_user_id", sa.Uuid()),
        sa.Column("source_module", sa.String(48), nullable=False),
        sa.Column("source_type", sa.String(64), nullable=False),
        sa.Column("source_id", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("content", postgresql.JSONB(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column("provider_message_id", sa.String(64)),
        sa.Column("first_attempted_at", sa.DateTime(timezone=True)),
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
        sa.CheckConstraint(
            "num_nonnulls(destination_id, recipient_user_id) = 1",
            name="ck_message_delivery_target",
        ),
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
    op.create_index(
        "ix_message_delivery_retry",
        "message_deliveries",
        ["first_attempted_at"],
        schema="keystone",
        postgresql_where=sa.text("status = 'unknown' AND attempts < 3"),
    )

    op.create_table(
        "message_inbounds",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "channel_id",
            sa.Uuid(),
            sa.ForeignKey("keystone.message_channels.id"),
            nullable=False,
        ),
        sa.Column("dedupe_key", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("delivery_id", sa.Uuid()),
        sa.Column("action_key", sa.String(64)),
        sa.Column("provider_message_id", sa.String(64)),
        sa.Column("external_user_id", sa.String(64), nullable=False),
        sa.Column("actor_id", sa.Uuid()),
        sa.Column("occurred_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("channel_id", "dedupe_key", name="uq_message_inbound_dedupe"),
        sa.CheckConstraint(
            "status IN ('received', 'processed', 'rejected', 'expired', 'failed')",
            name="ck_message_inbound_status",
        ),
        sa.CheckConstraint("kind IN ('card_action', 'bind')", name="ck_message_inbound_kind"),
        schema="keystone",
    )
    op.create_index(
        "ix_message_inbound_open",
        "message_inbounds",
        ["updated_at"],
        schema="keystone",
        postgresql_where=sa.text("status = 'received'"),
    )
    op.create_index(
        "ix_message_inbound_history",
        "message_inbounds",
        ["received_at", "id"],
        schema="keystone",
    )
    op.create_table(
        "message_bind_codes",
        sa.Column("user_id", sa.Uuid(), primary_key=True),
        sa.Column("code_digest", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("code_digest"),
        schema="keystone",
    )


def downgrade():
    op.drop_table("message_bind_codes", schema="keystone")
    op.drop_table("message_inbounds", schema="keystone")
    op.drop_table("message_deliveries", schema="keystone")
    op.drop_table("message_external_accounts", schema="keystone")
    op.drop_table("message_destinations", schema="keystone")
    op.drop_table("message_channels", schema="keystone")

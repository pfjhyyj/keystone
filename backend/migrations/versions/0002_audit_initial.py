"""Initialize the audit domain.

Tables: audit_logs.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("actor_type", sa.String(length=16), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("target_type", sa.String(length=100), nullable=True),
        sa.Column("target_id", sa.String(length=128), nullable=True),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=True),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.CheckConstraint("(actor_type = 'user') = (actor_id IS NOT NULL)"),
        sa.CheckConstraint("actor_type IN ('user', 'system', 'anonymous')"),
        sa.CheckConstraint("outcome IN ('success', 'failure', 'denied')"),
        sa.PrimaryKeyConstraint("id"),
        schema="keystone",
    )
    op.create_index(
        "ix_audit_actor_time",
        "audit_logs",
        ["actor_id", "occurred_at"],
        unique=False,
        schema="keystone",
    )
    op.create_index(
        "ix_audit_target_time",
        "audit_logs",
        ["target_type", "target_id", "occurred_at"],
        unique=False,
        schema="keystone",
    )
    op.create_index(
        "ix_audit_time", "audit_logs", ["occurred_at", "id"], unique=False, schema="keystone"
    )


def downgrade():
    op.drop_table("audit_logs", schema="keystone")

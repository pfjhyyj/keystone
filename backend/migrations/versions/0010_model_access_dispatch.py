"""Persist send spacing and cooldown by opaque actual credential scope."""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade():
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

"""Default model for each Flow model role.

Revision ID: 0010
Revises: 0009
"""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "flow_model_defaults",
        sa.Column("definition_key", sa.String(100), primary_key=True),
        sa.Column("role", sa.String(64), primary_key=True),
        sa.Column(
            "model_entry_id",
            sa.Uuid(),
            sa.ForeignKey("keystone.model_entries.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("updated_by", sa.Uuid()),
        schema="keystone",
    )
    op.create_index(
        "ix_flow_model_default_entry",
        "flow_model_defaults",
        ["model_entry_id"],
        schema="keystone",
    )


def downgrade():
    op.drop_index("ix_flow_model_default_entry", "flow_model_defaults", schema="keystone")
    op.drop_table("flow_model_defaults", schema="keystone")

"""Declare model operations without inferring capabilities from historical names."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "model_entries",
        sa.Column(
            "operations",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[\"text.generate\"]'::jsonb"),
            nullable=False,
        ),
        schema="keystone",
    )
    op.create_check_constraint(
        "model_entry_operations",
        "model_entries",
        "jsonb_typeof(operations) = 'array' AND jsonb_array_length(operations) BETWEEN 1 AND 5 "
        'AND operations <@ \'["text.generate","text.structured","image.understand",'
        '"image.generate","image.edit"]\'::jsonb',
        schema="keystone",
    )


def downgrade():
    op.drop_constraint("model_entry_operations", "model_entries", schema="keystone", type_="check")
    op.drop_column("model_entries", "operations", schema="keystone")

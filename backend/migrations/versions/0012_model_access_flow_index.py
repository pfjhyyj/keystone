"""Index Run-owned observations without rewriting historical context."""

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        "ix_model_invocations_flow_time",
        "model_invocations",
        [sa.text("(flow_context ->> 'flow_run_id')"), "created_at", "id"],
        schema="keystone",
    )


def downgrade():
    op.drop_index(
        "ix_model_invocations_flow_time", table_name="model_invocations", schema="keystone"
    )

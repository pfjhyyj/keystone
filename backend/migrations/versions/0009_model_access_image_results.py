"""Keep operation identity and bounded recovery evidence separate from billing."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "model_invocations",
        sa.Column(
            "operation",
            sa.String(32),
            nullable=False,
            server_default="text.generate",
        ),
        schema="keystone",
    )
    op.add_column(
        "model_invocations",
        sa.Column(
            "result_context",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        schema="keystone",
    )
    op.create_check_constraint(
        "invocation_operation",
        "model_invocations",
        "operation IN ('text.generate','text.structured','image.understand',"
        "'image.generate','image.edit')",
        schema="keystone",
    )
    op.create_check_constraint(
        "invocation_result_context",
        "model_invocations",
        "result_context IS NULL OR (jsonb_typeof(result_context) = 'object' "
        "AND octet_length(result_context::text) <= 16384)",
        schema="keystone",
    )


def downgrade():
    op.drop_constraint(
        "invocation_result_context", "model_invocations", schema="keystone", type_="check"
    )
    op.drop_constraint(
        "invocation_operation", "model_invocations", schema="keystone", type_="check"
    )
    op.drop_column("model_invocations", "result_context", schema="keystone")
    op.drop_column("model_invocations", "operation", schema="keystone")

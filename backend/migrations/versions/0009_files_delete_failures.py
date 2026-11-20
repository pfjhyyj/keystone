"""Persist the bounded retry count for file deletion."""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "files",
        sa.Column("delete_failures", sa.Integer(), nullable=False, server_default="0"),
        schema="keystone",
    )
    op.create_check_constraint(
        "file_delete_failures", "files", "delete_failures BETWEEN 0 AND 3", schema="keystone"
    )


def downgrade():
    op.drop_constraint("file_delete_failures", "files", type_="check", schema="keystone")
    op.drop_column("files", "delete_failures", schema="keystone")

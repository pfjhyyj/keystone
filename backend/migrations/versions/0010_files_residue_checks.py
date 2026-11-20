"""Bound residue checks of failed uploads and S3 deletion tombstones."""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "files",
        sa.Column("residue_checks", sa.Integer(), nullable=False, server_default="0"),
        schema="keystone",
    )
    op.create_check_constraint(
        "file_residue_checks", "files", "residue_checks BETWEEN 0 AND 3", schema="keystone"
    )


def downgrade():
    op.drop_constraint("file_residue_checks", "files", type_="check", schema="keystone")
    op.drop_column("files", "residue_checks", schema="keystone")

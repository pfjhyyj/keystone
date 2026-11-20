"""Persist bounded file cleanup counters: deletion failures and late residue checks."""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

COUNTERS = ("delete_failures", "residue_checks")


def upgrade():
    for name in COUNTERS:
        op.add_column(
            "files",
            sa.Column(name, sa.Integer(), nullable=False, server_default="0"),
            schema="keystone",
        )
        op.create_check_constraint(
            f"file_{name}", "files", f"{name} BETWEEN 0 AND 3", schema="keystone"
        )


def downgrade():
    for name in reversed(COUNTERS):
        op.drop_constraint(f"file_{name}", "files", type_="check", schema="keystone")
        op.drop_column("files", name, schema="keystone")

"""Let any role hold all current and future permissions; the admin role starts with it."""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "roles",
        sa.Column("all_permissions", sa.Boolean(), nullable=False, server_default=sa.false()),
        schema="keystone",
    )
    op.execute("UPDATE keystone.roles SET all_permissions = true WHERE key = 'admin'")
    op.execute(
        "DELETE FROM keystone.role_permissions WHERE role_id IN "
        "(SELECT id FROM keystone.roles WHERE all_permissions)"
    )


def downgrade():
    op.drop_column("roles", "all_permissions", schema="keystone")

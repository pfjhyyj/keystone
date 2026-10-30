"""Optional Alibaba workspace for native model discovery."""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "model_channels", sa.Column("workspace_id", sa.String(63), nullable=True), schema="keystone"
    )
    op.create_check_constraint(
        "channel_workspace",
        "model_channels",
        "workspace_id IS NULL OR (provider = 'aliyun_bailian' "
        "AND workspace_id ~ '^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$')",
        schema="keystone",
    )


def downgrade():
    op.drop_constraint("channel_workspace", "model_channels", schema="keystone", type_="check")
    op.drop_column("model_channels", "workspace_id", schema="keystone")

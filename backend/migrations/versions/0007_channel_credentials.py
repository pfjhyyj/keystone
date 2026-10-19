"""Store write-only channel credentials instead of deployment secret references."""

import sqlalchemy as sa
from alembic import op

revision = "0007_channel_credentials"
down_revision = "0006_flow"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("model_channels", sa.Column("api_key_encrypted", sa.Text()), schema="keystone")
    op.add_column(
        "model_channels", sa.Column("billing_api_key_encrypted", sa.Text()), schema="keystone"
    )
    # Reference names contain no recoverable API keys. Existing channels need manual entry.
    op.drop_column("model_channels", "credential_ref", schema="keystone")
    op.drop_column("model_channels", "billing_credential_ref", schema="keystone")


def downgrade():
    op.add_column(
        "model_channels",
        sa.Column("credential_ref", sa.String(64), nullable=False, server_default="unconfigured"),
        schema="keystone",
    )
    op.alter_column("model_channels", "credential_ref", server_default=None, schema="keystone")
    op.add_column(
        "model_channels", sa.Column("billing_credential_ref", sa.String(64)), schema="keystone"
    )
    op.drop_column("model_channels", "api_key_encrypted", schema="keystone")
    op.drop_column("model_channels", "billing_api_key_encrypted", schema="keystone")

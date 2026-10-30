"""Allow Volcengine Ark channels while preserving existing channel identities."""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("channel_provider", "model_channels", schema="keystone", type_="check")
    op.create_check_constraint(
        "channel_provider",
        "model_channels",
        "provider IN ('deepseek','openrouter','aliyun_bailian',"
        "'newapi','openlux','yunwu','volcengine')",
        schema="keystone",
    )


def downgrade():
    # Existing Ark channels must be handled explicitly before downgrading.
    op.drop_constraint("channel_provider", "model_channels", schema="keystone", type_="check")
    op.create_check_constraint(
        "channel_provider",
        "model_channels",
        "provider IN ('deepseek','openrouter','aliyun_bailian','newapi','openlux','yunwu')",
        schema="keystone",
    )

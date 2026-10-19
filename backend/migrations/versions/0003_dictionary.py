"""Initialize the dictionary domain.

Tables: dictionaries, dictionary_items.
"""

import sqlalchemy as sa
from alembic import op

revision = "0003_dictionary"
down_revision = "0002_audit"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "dictionaries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.String(length=1000), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key"),
        schema="keystone",
    )
    op.create_table(
        "dictionary_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("dictionary_id", sa.Uuid(), nullable=False),
        sa.Column("value", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=128), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["dictionary_id"],
            ["keystone.dictionaries.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dictionary_id", "value"),
        schema="keystone",
    )


def downgrade():
    op.drop_table("dictionary_items", schema="keystone")
    op.drop_table("dictionaries", schema="keystone")

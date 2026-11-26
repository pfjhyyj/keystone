"""Bind one PostgreSQL database to its deployment topology.

Revision ID: 0011
Revises: 0010
"""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "deployment_binding",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=False),
        sa.Column("topology", sa.String(16), nullable=False),
        sa.Column("host_id", sa.String(255)),
        sa.Column("coordination_id", sa.String(64)),
        sa.Column("credential_key_fingerprint", sa.String(64)),
        sa.CheckConstraint("id = 1", name="deployment_binding_singleton"),
        sa.CheckConstraint(
            "(topology = 'filesystem' AND host_id IS NOT NULL AND coordination_id IS NOT NULL "
            "AND credential_key_fingerprint IS NULL) OR "
            "(topology = 'shared' AND host_id IS NULL AND coordination_id IS NULL "
            "AND credential_key_fingerprint IS NOT NULL)",
            name="deployment_binding_topology",
        ),
        schema="keystone",
    )


def downgrade():
    op.drop_table("deployment_binding", schema="keystone")

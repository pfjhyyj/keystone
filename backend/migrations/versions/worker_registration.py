"""Durable Worker membership, independent of process heartbeats."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "worker_registration"
down_revision = "multi_workers"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "worker_registrations",
        sa.Column("worker_id", sa.String(64), primary_key=True),
        sa.Column("state", sa.String(16), nullable=False, server_default="registered"),
        sa.Column("host_id", sa.String(255)),
        sa.Column("coordination_id", sa.String(64)),
        sa.Column("execution_version", sa.String(64)),
        sa.Column("config_digest", sa.String(64)),
        sa.Column("instance_id", UUID()),
        sa.Column(
            "registered_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.clock_timestamp(),
        ),
        sa.Column("stopped_at", sa.DateTime(timezone=True)),
        sa.Column("retired_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("state IN ('registered','retired')", name="worker_registration_state"),
        sa.CheckConstraint("(state = 'retired') = (retired_at IS NOT NULL)", name="worker_retired"),
        schema="keystone",
    )
    # Preserve offline/stopped identities as members, never infer retirement from heartbeat age.
    op.execute("""
        INSERT INTO keystone.worker_registrations
          (worker_id,host_id,coordination_id,execution_version,config_digest,instance_id,
           registered_at,stopped_at)
        SELECT DISTINCT ON (worker_id)
          worker_id,host_id,coordination_id,execution_version,config_digest,instance_id,
          started_at,CASE WHEN state='stopped' THEN heartbeat_at END
        FROM keystone.worker_heartbeats
        ORDER BY worker_id,started_at DESC,instance_id
    """)
    # A running call can outlive retention of its heartbeat; preserve its recovery identity.
    op.execute("""
        INSERT INTO keystone.worker_registrations(worker_id)
        SELECT DISTINCT worker_id FROM keystone.model_invocations
        WHERE worker_id IS NOT NULL AND status='running'
        ON CONFLICT (worker_id) DO NOTHING
    """)
    for worker_id in op.get_context().config.attributes.get("legacy_worker_ids", []):
        op.get_bind().execute(
            sa.text(
                "INSERT INTO keystone.worker_registrations(worker_id) VALUES (:id) "
                "ON CONFLICT (worker_id) DO NOTHING"
            ),
            {"id": worker_id},
        )


def downgrade():
    op.drop_table("worker_registrations", schema="keystone")

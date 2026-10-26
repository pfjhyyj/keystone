"""Permit controlled text embedding operations without changing existing records."""

from alembic import op
from sqlalchemy import text

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE keystone.model_entries DROP CONSTRAINT model_entry_operations")
    op.execute("""ALTER TABLE keystone.model_entries ADD CONSTRAINT model_entry_operations
        CHECK (jsonb_typeof(operations) = 'array' AND jsonb_array_length(operations) BETWEEN 1 AND 6
        AND operations <@ '["text.generate","text.structured","image.understand",
        "image.generate","image.edit","text.embed"]'::jsonb)""")
    op.execute("ALTER TABLE keystone.model_invocations DROP CONSTRAINT invocation_operation")
    op.execute("""ALTER TABLE keystone.model_invocations ADD CONSTRAINT invocation_operation
        CHECK (operation IN ('text.generate','text.structured','image.understand',
        'image.generate','image.edit','text.embed'))""")


def downgrade():
    if (
        op.get_bind()
        .execute(
            text("""SELECT EXISTS (
        SELECT 1 FROM keystone.model_entries WHERE operations @> '["text.embed"]'::jsonb
        UNION ALL SELECT 1 FROM keystone.model_invocations WHERE operation = 'text.embed'
    )""")
        )
        .scalar()
    ):
        raise RuntimeError("Embedding records require restoring the earlier database snapshot")
    op.execute("ALTER TABLE keystone.model_entries DROP CONSTRAINT model_entry_operations")
    op.execute("""ALTER TABLE keystone.model_entries ADD CONSTRAINT model_entry_operations
        CHECK (jsonb_typeof(operations) = 'array' AND jsonb_array_length(operations) BETWEEN 1 AND 5
        AND operations <@ '["text.generate","text.structured","image.understand",
        "image.generate","image.edit"]'::jsonb)""")
    op.execute("ALTER TABLE keystone.model_invocations DROP CONSTRAINT invocation_operation")
    op.execute("""ALTER TABLE keystone.model_invocations ADD CONSTRAINT invocation_operation
        CHECK (operation IN ('text.generate','text.structured','image.understand',
        'image.generate','image.edit'))""")

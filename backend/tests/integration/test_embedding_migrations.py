"""The model baseline supports embeddings and preserves their facts across the chain."""

import pytest
from alembic import command
from sqlalchemy import text
from test_model_flow_migrations import migration_environment as migration_environment
from test_model_flow_migrations import model_baseline as model_baseline

pytestmark = pytest.mark.postgres


def test_embedding_baseline_roundtrip_preserves_existing_records(model_baseline):
    config, engine = model_baseline
    tables = ("model_channels", "model_entries", "model_invocations", "model_dispatch_scopes")

    def facts():
        with engine.connect() as connection:
            return {
                name: connection.execute(text(f"SELECT * FROM keystone.{name}")).mappings().all()
                for name in tables
            }

    with engine.begin() as connection:
        assert connection.scalar(text("SELECT version_num FROM keystone.alembic_version")) == "0005"
        connection.execute(
            text("""INSERT INTO keystone.model_entries
        (id,key,channel_id,provider_model,operations,is_active)
        VALUES ('00000000-0000-4000-8000-000000000006','vectors',
        '00000000-0000-4000-8000-000000000001','explicit', '["text.embed"]',true)""")
        )
        connection.execute(
            text("""INSERT INTO keystone.model_invocations
        (id,model_entry_id,channel_id,provider_model,operation,status,finished_at,
         cost_source,cost_context,receipt_ids)
        VALUES ('00000000-0000-4000-8000-000000000007',
        '00000000-0000-4000-8000-000000000006',
        '00000000-0000-4000-8000-000000000001','explicit','text.embed','unknown',now(),
        'unknown','{}','{}')""")
        )
    with_embedding = facts()
    for _ in range(2):
        command.upgrade(config, "head")
        assert facts() == with_embedding
        command.downgrade(config, "0005")
        assert facts() == with_embedding
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM keystone.alembic_version"))
                == "0005"
            )

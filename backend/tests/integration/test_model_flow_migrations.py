"""Historical model/Flow migrations preserve facts without application head fixtures."""

from uuid import UUID

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text

from keystone.core.config import Settings
from keystone.core.migrations import migration_config

pytestmark = pytest.mark.postgres


@pytest.fixture
def migration_environment(empty_database_url):
    config = migration_config(Settings(config_file=None, database_url=empty_database_url))
    config.attributes["create_schema"] = True
    engine = create_engine(empty_database_url)
    try:
        yield config, engine
    finally:
        engine.dispose()


def test_operations_upgrade_preserves_existing_models_and_uses_conservative_default(
    migration_environment,
):
    config, engine = migration_environment
    command.upgrade(config, "0007")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM keystone.alembic_version")) == "0007"
        assert "operations" not in {
            c["name"] for c in inspect(connection).get_columns("model_entries", schema="keystone")
        }
    channel_id, entry_id = (
        "00000000-0000-4000-8000-000000000001",
        "00000000-0000-4000-8000-000000000002",
    )
    with engine.begin() as connection:
        connection.execute(
            text("""INSERT INTO keystone.model_channels
            (id,name,provider,api_base,api_key_encrypted,is_active,balance_check_status)
            VALUES (:id,'existing','openrouter','https://openrouter.ai/api/v1','opaque',true,'never')"""),
            {"id": UUID(channel_id)},
        )
        connection.execute(
            text("""INSERT INTO keystone.model_entries
            (id,key,channel_id,provider_model,is_active)
            VALUES (:id,'existing',:channel_id,'unknown-image-model-name',true)"""),
            {"id": UUID(entry_id), "channel_id": UUID(channel_id)},
        )
    command.upgrade(config, "0008")
    with engine.connect() as connection:
        assert connection.execute(
            text("""SELECT key,provider_model,operations
            FROM keystone.model_entries WHERE id=:id"""),
            {"id": UUID(entry_id)},
        ).one() == ("existing", "unknown-image-model-name", ["text.generate"])
    command.downgrade(config, "0007")
    command.upgrade(config, "0008")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT operations FROM keystone.model_entries")) == [
            "text.generate"
        ]


def test_image_evidence_upgrade_preserves_historical_invocation_and_billing(migration_environment):
    config, engine = migration_environment
    command.upgrade(config, "0008")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM keystone.alembic_version")) == "0008"
        assert "operation" not in {
            c["name"]
            for c in inspect(connection).get_columns("model_invocations", schema="keystone")
        }
    with engine.begin() as connection:
        connection.execute(
            text("""INSERT INTO keystone.model_channels
            (id,name,provider,api_base,api_key_encrypted,is_active,balance_check_status)
            VALUES ('00000000-0000-4000-8000-000000000001','old','openrouter',
            'https://openrouter.ai/api/v1','opaque',true,'never')""")
        )
        connection.execute(
            text("""INSERT INTO keystone.model_entries
            (id,key,channel_id,provider_model,is_active)
            VALUES ('00000000-0000-4000-8000-000000000002','old',
            '00000000-0000-4000-8000-000000000001','old-model',true)""")
        )
        connection.execute(
            text("""INSERT INTO keystone.model_invocations
            (id,model_entry_id,channel_id,provider_model,status,finished_at,
             cost_amount,cost_unit,cost_source,cost_context,receipt_ids,usage)
            VALUES ('00000000-0000-4000-8000-000000000003',
            '00000000-0000-4000-8000-000000000002',
            '00000000-0000-4000-8000-000000000001','old-model','unknown',now(),
            0.123456789012345678,'USD','query','{"provider":"openrouter"}',
            '{"header":"original-receipt"}','{"total_tokens": 17}')""")
        )
        before = (
            connection.execute(text("SELECT * FROM keystone.model_invocations")).mappings().one()
        )
        before = dict(before)
    for _ in range(2):
        command.upgrade(config, "0009")
        with engine.connect() as connection:
            after = dict(
                connection.execute(text("SELECT * FROM keystone.model_invocations"))
                .mappings()
                .one()
            )
            assert after.pop("operation") == "text.generate"
            assert after.pop("result_context") is None
            assert after == before
        command.downgrade(config, "0008")
        with engine.connect() as connection:
            assert (
                dict(
                    connection.execute(text("SELECT * FROM keystone.model_invocations"))
                    .mappings()
                    .one()
                )
                == before
            )


def test_budget_migration_preserves_old_run_without_backfilling_budget(migration_environment):
    config, engine = migration_environment
    command.upgrade(config, "0010")
    with engine.begin() as connection:
        assert "flow_run_budgets" not in inspect(connection).get_table_names(schema="keystone")
        connection.execute(
            text("""INSERT INTO keystone.users
            (id,username,display_name,password_hash,is_active,password_change_required)
            VALUES ('00000000-0000-4000-8000-000000000001',
            'historic','Original','opaque',true,false)""")
        )
        connection.execute(
            text("""INSERT INTO keystone.flow_runs
            (id,definition_key,definition_snapshot,execution_version,actor_id,
            request_key,request_digest,input,status,finished_at)
            VALUES ('00000000-0000-4000-8000-000000000002','historic.flow',
            '{"output_schema": {"type": "object"}, "output_file_fields": []}',
            'original-artifact','00000000-0000-4000-8000-000000000001',
            'historic-request','historic-digest','{}','failed',now())""")
        )
        before = dict(connection.execute(text("SELECT * FROM keystone.flow_runs")).mappings().one())
    for _ in range(2):
        command.upgrade(config, "0011")
        with engine.connect() as connection:
            assert (
                dict(connection.execute(text("SELECT * FROM keystone.flow_runs")).mappings().one())
                == before
            )
            assert connection.scalar(text("SELECT count(*) FROM keystone.flow_run_budgets")) == 0
            assert connection.scalar(text("SELECT count(*) FROM keystone.flow_budget_claims")) == 0
        command.downgrade(config, "0010")
        with engine.connect() as connection:
            assert (
                dict(connection.execute(text("SELECT * FROM keystone.flow_runs")).mappings().one())
                == before
            )


def test_flow_index_upgrade_and_downgrade_preserve_original_context(migration_environment):
    config, engine = migration_environment
    command.upgrade(config, "0011")
    with engine.begin() as connection:
        connection.execute(
            text("""INSERT INTO keystone.model_channels
        (id,name,provider,api_base,api_key_encrypted,is_active,balance_check_status)
        VALUES ('00000000-0000-4000-8000-000000000001','old','openrouter',
        'https://openrouter.ai/api/v1','opaque',true,'never')""")
        )
        connection.execute(
            text("""INSERT INTO keystone.model_entries
        (id,key,channel_id,provider_model,is_active)
        VALUES ('00000000-0000-4000-8000-000000000002','old',
        '00000000-0000-4000-8000-000000000001','old-model',true)""")
        )
        connection.execute(
            text("""INSERT INTO keystone.model_invocations
        (id,model_entry_id,channel_id,provider_model,status,finished_at,cost_source,flow_context)
        VALUES ('00000000-0000-4000-8000-000000000003',
        '00000000-0000-4000-8000-000000000002','00000000-0000-4000-8000-000000000001',
        'old-model','unknown',now(),'unknown','{"flow_run_id":"original","node_key":"old-node"}')""")
        )
        before = dict(
            connection.execute(text("SELECT * FROM keystone.model_invocations")).mappings().one()
        )
    for _ in range(2):
        command.upgrade(config, "0012")
        with engine.connect() as connection:
            index = connection.scalar(
                text(
                    "SELECT indexdef FROM pg_indexes WHERE schemaname='keystone' "
                    "AND indexname='ix_model_invocations_flow_time'"
                )
            )
            assert "flow_context ->> 'flow_run_id'" in index and "created_at, id" in index
            assert (
                dict(
                    connection.execute(text("SELECT * FROM keystone.model_invocations"))
                    .mappings()
                    .one()
                )
                == before
            )
        command.downgrade(config, "0011")
        with engine.connect() as connection:
            assert (
                dict(
                    connection.execute(text("SELECT * FROM keystone.model_invocations"))
                    .mappings()
                    .one()
                )
                == before
            )

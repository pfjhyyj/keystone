"""Current model/Flow baselines preserve facts across one reversible roundtrip."""

import json
from decimal import Decimal

import pytest
from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

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


@pytest.fixture
def model_baseline(migration_environment):
    config, engine = migration_environment
    command.upgrade(config, "0005")
    with engine.begin() as connection:
        connection.execute(
            text("""INSERT INTO keystone.model_channels
            (id,name,provider,api_base,api_key_encrypted,is_active,balance_check_status)
            VALUES ('00000000-0000-4000-8000-000000000001','original','newapi',
            'https://newapiai.com/v1','opaque',true,'never')""")
        )
        connection.execute(
            text("""INSERT INTO keystone.model_entries
            (id,key,channel_id,provider_model,is_active)
            VALUES ('00000000-0000-4000-8000-000000000002','original',
            '00000000-0000-4000-8000-000000000001','unknown-image-model-name',true)""")
        )
        connection.execute(
            text("""INSERT INTO keystone.model_invocations
            (id,model_entry_id,channel_id,provider_model,status,finished_at,
             cost_amount,cost_unit,cost_source,cost_context,receipt_ids,usage,flow_context)
            VALUES ('00000000-0000-4000-8000-000000000003',
            '00000000-0000-4000-8000-000000000002',
            '00000000-0000-4000-8000-000000000001','original','unknown',now(),
            0.123456789012345678,'USD','query','{"provider":"openrouter"}',
            '{"header":"original-receipt"}','{"total_tokens": 17}',
            '{"flow_run_id":"original","node_key":"original-node"}')""")
        )
        connection.execute(
            text("""INSERT INTO keystone.model_invocations
            (id,model_entry_id,channel_id,provider_model,status,finished_at,cost_source,
             operation,result_context,cost_evidence)
            VALUES ('00000000-0000-4000-8000-000000000004',
            '00000000-0000-4000-8000-000000000002',
            '00000000-0000-4000-8000-000000000001','original','unknown',now(),'unknown',
            'image.generate','{"task_id":"original-task"}',
            '{"receipt":"original-cost-evidence"}')""")
        )
        connection.execute(
            text("INSERT INTO keystone.model_dispatch_scopes (scope) VALUES (:scope)"),
            {"scope": "a" * 64},
        )
        connection.execute(
            text("""INSERT INTO keystone.model_entries
            (id,key,name,channel_id,provider_model,operations,is_active)
            VALUES ('00000000-0000-4000-8000-000000000006','vectors','Vectors',
            '00000000-0000-4000-8000-000000000001','explicit','["text.embed"]',true)""")
        )
        connection.execute(
            text("""INSERT INTO keystone.model_invocations
            (id,model_entry_id,channel_id,provider_model,operation,status,finished_at,cost_source)
            VALUES ('00000000-0000-4000-8000-000000000007',
            '00000000-0000-4000-8000-000000000006',
            '00000000-0000-4000-8000-000000000001','explicit','text.embed','unknown',now(),'unknown')""")
        )
    return config, engine


def test_model_baseline_defaults_and_roundtrip_preserve_billing_and_recovery(model_baseline):
    config, engine = model_baseline
    tables = ("model_channels", "model_entries", "model_invocations", "model_dispatch_scopes")
    with engine.connect() as connection:
        before = {
            table: connection.execute(text(f"SELECT * FROM keystone.{table}")).mappings().all()
            for table in tables
        }
        entry = next(row for row in before["model_entries"] if row["key"] == "original")
        assert entry["operations"] == ["text.generate"]
        assert entry["pricing"] == {"mode": "inherit", "rules": []}
        assert entry["pricing_revision"] == 0
        assert before["model_channels"][0]["workspace_id"] is None
        assert before["model_channels"][0]["provider"] == "newapi"
        vectors = next(row for row in before["model_entries"] if row["key"] == "vectors")
        assert vectors["name"] == "Vectors" and vectors["operations"] == ["text.embed"]
        invocations = {row["operation"]: row for row in before["model_invocations"]}
        assert invocations["text.embed"]["result_context"] is None
        assert invocations["text.generate"]["result_context"] is None
        assert invocations["text.generate"]["cost_amount"] == Decimal("0.123456789012345678")
        assert invocations["image.generate"]["result_context"] == {"task_id": "original-task"}
        assert {row["status"] for row in invocations.values()} == {"unknown"}
        assert all(row["estimate_snapshot"] is None for row in invocations.values())
        assert {row["estimate_status"] for row in invocations.values()} == {"unavailable"}
        assert {row["estimate_reason"] for row in invocations.values()} == {"snapshot_missing"}
    command.upgrade(config, "head")
    command.downgrade(config, "0005")
    with engine.connect() as connection:
        for table in tables:
            assert (
                connection.execute(text(f"SELECT * FROM keystone.{table}")).mappings().all()
                == before[table]
            )
        index = connection.scalar(
            text("""SELECT indexdef FROM pg_indexes WHERE schemaname='keystone'
            AND indexname='ix_model_invocations_flow_time'""")
        )
        assert "flow_context ->> 'flow_run_id'" in index and "created_at, id" in index


@pytest.mark.parametrize(
    "table,column,value",
    [
        ("model_entries", "operations", "[]"),
        ("model_entries", "operations", "{}"),
        ("model_entries", "operations", "null"),
        ("model_invocations", "result_context", "[]"),
        ("model_invocations", "result_context", json.dumps({"task_id": "x" * 16384})),
        ("model_dispatch_scopes", "scope", "invalid-scope"),
    ],
    ids=[
        "empty-operations",
        "object-operations",
        "json-null-operations",
        "nonobject-evidence",
        "oversized-evidence",
        "invalid-dispatch-scope",
    ],
)
def test_model_baseline_enforces_operation_and_evidence_bounds(
    model_baseline, table, column, value
):
    _, engine = model_baseline
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(text(f"UPDATE keystone.{table} SET {column}=:value"), {"value": value})


def test_flow_baseline_roundtrip_preserves_current_snapshot_and_budget(migration_environment):
    from support.flow_fixture.contracts import definition
    from support.flow_packages import snapshot_for

    from keystone.platform.flow.schemas.budgets import BudgetCounters

    config, engine = migration_environment
    command.upgrade(config, "0006")
    snapshot = snapshot_for(definition)
    counters = BudgetCounters().model_dump()
    with engine.begin() as connection:
        connection.execute(
            text("""INSERT INTO keystone.users
            (id,username,display_name,password_hash,is_active,password_change_required)
            VALUES ('00000000-0000-4000-8000-000000000001',
            'current','Current','opaque',true,false)""")
        )
        connection.execute(
            text("""INSERT INTO keystone.flow_runs
            (id,definition_key,definition_snapshot,execution_version,actor_id,
             request_key,request_digest,input,status,finished_at)
            VALUES ('00000000-0000-4000-8000-000000000002',:key,CAST(:snapshot AS jsonb),
            'original-artifact','00000000-0000-4000-8000-000000000001',
            'current-request','current-digest','{}','failed',now())"""),
            {"key": definition.key, "snapshot": json.dumps(snapshot)},
        )
        connection.execute(
            text("""INSERT INTO keystone.flow_run_budgets (run_id,limits,counters)
            VALUES ('00000000-0000-4000-8000-000000000002',
                    CAST(:limits AS jsonb),CAST(:counters AS jsonb))"""),
            {"limits": json.dumps(snapshot["limits"]), "counters": json.dumps(counters)},
        )
        connection.execute(
            text("""INSERT INTO keystone.flow_budget_claims (run_id,kind,key,amounts,request_digest)
            VALUES ('00000000-0000-4000-8000-000000000002','model',:key,
                    CAST(:amounts AS jsonb),:digest)"""),
            {"key": "a" * 64, "digest": "b" * 64, "amounts": json.dumps(counters)},
        )
        tables = ("users", "flow_runs", "flow_run_budgets", "flow_budget_claims")
        before = {
            table: connection.execute(text(f"SELECT * FROM keystone.{table}")).mappings().all()
            for table in tables
        }
    command.upgrade(config, "head")
    command.downgrade(config, "0006")
    with engine.connect() as connection:
        for table in tables:
            assert (
                connection.execute(text(f"SELECT * FROM keystone.{table}")).mappings().all()
                == before[table]
            )

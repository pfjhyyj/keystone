"""Complete model/Flow baselines and safe rejection of retired revision chains."""

import json
from decimal import Decimal

import pytest
from alembic import command
from alembic.util.exc import CommandError
from sqlalchemy import create_engine, inspect, text
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
            VALUES ('00000000-0000-4000-8000-000000000001','original','openrouter',
            'https://openrouter.ai/api/v1','opaque',true,'never')""")
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
    return config, engine


def test_model_baseline_defaults_and_roundtrip_preserve_billing_and_recovery(model_baseline):
    config, engine = model_baseline
    tables = ("model_channels", "model_entries", "model_invocations", "model_dispatch_scopes")
    with engine.connect() as connection:
        before = {
            table: connection.execute(text(f"SELECT * FROM keystone.{table}")).mappings().all()
            for table in tables
        }
        assert before["model_entries"][0]["operations"] == ["text.generate"]
        invocations = {row["operation"]: row for row in before["model_invocations"]}
        assert invocations["text.generate"]["result_context"] is None
        assert invocations["text.generate"]["cost_amount"] == Decimal("0.123456789012345678")
        assert invocations["image.generate"]["result_context"] == {"task_id": "original-task"}
        assert {row["status"] for row in invocations.values()} == {"unknown"}
    for _ in range(2):
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


def test_model_baseline_contains_newapi_and_editable_display_name(model_baseline):
    config, engine = model_baseline
    with engine.begin() as connection:
        connection.execute(
            text("""UPDATE keystone.model_channels SET provider='newapi',
            api_base='https://newapiai.com/v1'""")
        )
        connection.execute(text("UPDATE keystone.model_entries SET name='Display name'"))
        before = dict(
            connection.execute(text("SELECT * FROM keystone.model_entries")).mappings().one()
        )
        assert before["key"] == "original" and before["name"] == "Display name"
    command.upgrade(config, "head")
    command.downgrade(config, "0005")
    with engine.connect() as connection:
        assert (
            dict(connection.execute(text("SELECT * FROM keystone.model_entries")).mappings().one())
            == before
        )
        assert connection.scalar(text("SELECT provider FROM keystone.model_channels")) == "newapi"


@pytest.mark.parametrize(
    "table,column,value",
    [
        ("model_entries", "operations", "[]"),
        ("model_entries", "operations", '["video.generate"]'),
        ("model_invocations", "operation", "video.generate"),
        ("model_invocations", "result_context", "[]"),
        ("model_invocations", "result_context", json.dumps({"task_id": "x" * 16384})),
        ("model_dispatch_scopes", "scope", "invalid-scope"),
    ],
    ids=[
        "empty-operations",
        "unsupported-operation",
        "unsupported-invocation",
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


@pytest.mark.parametrize("with_budget", [False, True])
def test_flow_baseline_roundtrip_preserves_snapshots_and_explicit_budgets(
    migration_environment, with_budget
):
    config, engine = migration_environment
    command.upgrade(config, "0006")
    with engine.begin() as connection:
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
            '{"output_schema":{"type":"object"},"output_file_fields":[]}',
            'original-artifact','00000000-0000-4000-8000-000000000001',
            'historic-request','historic-digest','{}','failed',now())""")
        )
        if with_budget:
            counters = dict.fromkeys(
                (
                    "model_requests",
                    "tool_calls",
                    "iterations",
                    "file_count",
                    "file_bytes",
                    "input_tokens",
                    "output_tokens",
                    "unreported_text_requests",
                ),
                0,
            )
            limits = {key: 10 for key in counters if key != "unreported_text_requests"}
            limits.update(concurrent_operations=2, temporary_bytes=1024)
            connection.execute(
                text("""INSERT INTO keystone.flow_run_budgets (run_id,limits,counters)
                VALUES ('00000000-0000-4000-8000-000000000002',
                CAST(:limits AS jsonb),CAST(:counters AS jsonb))"""),
                {"limits": json.dumps(limits), "counters": json.dumps(counters)},
            )
            connection.execute(
                text("""INSERT INTO keystone.flow_budget_claims
                (run_id,kind,key,amounts,request_digest)
                VALUES ('00000000-0000-4000-8000-000000000002','model',:key,
                CAST(:amounts AS jsonb),:digest)"""),
                {"key": "a" * 64, "digest": "b" * 64, "amounts": json.dumps(counters)},
            )
        tables = ("users", "flow_runs", "flow_run_budgets", "flow_budget_claims")
        before = {
            table: connection.execute(text(f"SELECT * FROM keystone.{table}")).mappings().all()
            for table in tables
        }
        assert len(before["flow_run_budgets"]) == int(with_budget)
        assert len(before["flow_budget_claims"]) == int(with_budget)
    for _ in range(2):
        command.upgrade(config, "head")
        command.downgrade(config, "0006")
        with engine.connect() as connection:
            for table in tables:
                assert (
                    connection.execute(text(f"SELECT * FROM keystone.{table}")).mappings().all()
                    == before[table]
                )


@pytest.mark.parametrize(
    "revision",
    [
        *(f"{number:04d}" for number in range(8, 13)),
        "identity",
        "audit",
        "dictionary",
        "files",
        "model_access",
        "flow",
        "core",
        "model_costs",
        "multi_workers",
        "worker_registration",
    ],
)
def test_retired_revisions_fail_without_stamping_or_changing_data(migration_environment, revision):
    config, engine = migration_environment
    with engine.begin() as connection:
        connection.execute(text("CREATE SCHEMA keystone"))
        connection.execute(text("CREATE TABLE keystone.alembic_version (version_num varchar(32))"))
        connection.execute(
            text("INSERT INTO keystone.alembic_version VALUES (:revision)"), {"revision": revision}
        )
        connection.execute(text("CREATE TABLE keystone.retention_probe (value integer)"))
        connection.execute(text("INSERT INTO keystone.retention_probe VALUES (42)"))
        connection.execute(text("CREATE SCHEMA dbos"))
        connection.execute(text("CREATE TABLE dbos.retention_probe (value integer)"))
        connection.execute(text("INSERT INTO dbos.retention_probe VALUES (43)"))
    for action, target in ((command.upgrade, "head"), (command.downgrade, "base")):
        with pytest.raises(CommandError, match="Can't locate revision"):
            action(config, target)
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM keystone.alembic_version"))
                == revision
            )
            assert set(inspect(connection).get_table_names(schema="keystone")) == {
                "alembic_version",
                "retention_probe",
            }
            assert connection.scalar(text("SELECT value FROM keystone.retention_probe")) == 42
            assert connection.scalar(text("SELECT value FROM dbos.retention_probe")) == 43

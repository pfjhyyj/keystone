"""The model baseline keeps its database-level operation and evidence bounds."""

import json

import pytest
from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from keystone.core.config import Settings
from keystone.core.migrations import migration_config

pytestmark = pytest.mark.postgres


@pytest.fixture
def model_baseline(empty_database_url):
    config = migration_config(Settings(config_file=None, database_url=empty_database_url))
    config.attributes["create_schema"] = True
    command.upgrade(config, "0005")
    engine = create_engine(empty_database_url)
    try:
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
                '00000000-0000-4000-8000-000000000001','original',true)""")
            )
            connection.execute(
                text("""INSERT INTO keystone.model_invocations
                (id,model_entry_id,channel_id,provider_model,status,finished_at,cost_source)
                VALUES ('00000000-0000-4000-8000-000000000003',
                '00000000-0000-4000-8000-000000000002',
                '00000000-0000-4000-8000-000000000001','original','unknown',now(),'unknown')""")
            )
            connection.execute(
                text("INSERT INTO keystone.model_dispatch_scopes (scope) VALUES (:scope)"),
                {"scope": "a" * 64},
            )
        yield engine
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "table,column,value",
    [
        ("model_entries", "operations", "[]"),
        ("model_invocations", "result_context", json.dumps({"task_id": "x" * 16384})),
        ("model_dispatch_scopes", "scope", "invalid-scope"),
    ],
    ids=["empty-operations", "oversized-evidence", "invalid-dispatch-scope"],
)
def test_model_baseline_enforces_operation_and_evidence_bounds(
    model_baseline, table, column, value
):
    with pytest.raises(IntegrityError), model_baseline.begin() as connection:
        connection.execute(text(f"UPDATE keystone.{table} SET {column}=:value"), {"value": value})

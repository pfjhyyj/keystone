"""Upgrade old calls without fabricating historical tariffs or supplier receipts."""

import pytest
from alembic import command
from sqlalchemy import inspect, text
from test_model_flow_migrations import migration_environment as migration_environment
from test_model_flow_migrations import model_baseline as model_baseline

pytestmark = pytest.mark.postgres


def test_pricing_upgrade_and_downgrade_preserve_real_receipts_and_mark_legacy_calls(model_baseline):
    config, engine = model_baseline
    command.upgrade(config, "0009")

    def old_facts():
        with engine.connect() as connection:
            return connection.execute(
                text(
                    "SELECT id,cost_amount,cost_unit,cost_source,usage "
                    "FROM keystone.model_invocations ORDER BY id"
                )
            ).all()

    original = old_facts()
    for _ in range(2):
        command.upgrade(config, "head")
        assert old_facts() == original
        with engine.connect() as connection:
            entries = connection.execute(
                text("SELECT pricing,pricing_revision FROM keystone.model_entries")
            ).all()
            assert all(
                pricing == {"mode": "inherit", "rules": []} and revision == 0
                for pricing, revision in entries
            )
            rows = connection.execute(
                text(
                    "SELECT estimate_snapshot,estimate_status,estimate_amount,estimate_reason "
                    "FROM keystone.model_invocations"
                )
            ).all()
            assert all(
                snapshot is None
                and status == "unavailable"
                and amount is None
                and reason == "legacy_no_snapshot"
                for snapshot, status, amount, reason in rows
            )
        command.downgrade(config, "0009")
        assert old_facts() == original
        assert "estimate_snapshot" not in {
            c["name"] for c in inspect(engine).get_columns("model_invocations", schema="keystone")
        }

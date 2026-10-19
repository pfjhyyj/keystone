"""Public projections must stay explicit when an ORM row gains internal columns."""

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from keystone.platform.dictionary.schemas import item_view
from keystone.platform.dictionary.schemas import view as dictionary_view
from keystone.platform.files.schemas import view as file_view
from keystone.platform.flow.schemas import view as run_view
from keystone.platform.model_access.schemas import channel_view, entry_view, invocation_view

CASES = [
    (
        dictionary_view,
        "id key name description is_active created_at created_by updated_at updated_by",
    ),
    (
        item_view,
        """id dictionary_id value label sort_order is_active
                  created_at created_by updated_at updated_by""",
    ),
    (
        channel_view,
        """id name provider api_base has_api_key has_billing_api_key
                     upstream_account_ref is_active balance_snapshot balance_checked_at
                     balance_last_attempt_at balance_check_status created_at created_by
                     updated_at updated_by""",
    ),
    (
        entry_view,
        """id key channel_id provider_model is_active created_at created_by
                   updated_at updated_by""",
    ),
    (
        invocation_view,
        """id user_id flow_context model_entry_id channel_id provider_model
                        upstream_request_id status finished_at usage cost_amount cost_unit
                        cost_source cost_note error_code created_at updated_at""",
    ),
    (
        run_view,
        """id definition_key definition_snapshot execution_version actor_id
                 personal_access_token_id request_key request_digest input created_at status
                 result error_code cancel_requested_at finished_at observed_at""",
    ),
    (
        file_view,
        """id name owner_id content_type storage_key size_bytes checksum_sha256 status
                 deleted_at created_at created_by updated_at updated_by""",
    ),
]


class Row(SimpleNamespace):
    @property
    def __table__(self):
        raise AssertionError("An HTTP response must not enumerate ORM columns")


@pytest.mark.parametrize("project,contract", CASES, ids=[p.__module__ for p, _ in CASES])
def test_projection_ignores_new_internal_fields(project, contract):
    expected = set(contract.split())
    row = Row(**dict.fromkeys(expected), future_secret="never return this")
    result = project(row)
    assert set(result) == expected
    assert "future_secret" not in result


def test_cost_decimal_and_run_timezone_keep_wire_semantics():
    invocation = Row(**dict.fromkeys(CASES[4][1].split()))
    invocation.cost_amount = Decimal("0.000000001234")
    assert invocation_view(invocation)["cost_amount"] == "0.000000001234"
    run = Row(**dict.fromkeys(CASES[5][1].split()))
    run.created_at = datetime(2026, 10, 19, 8, tzinfo=timezone(timedelta(hours=8)))
    assert run_view(run)["created_at"] == datetime(2026, 10, 19, tzinfo=UTC)

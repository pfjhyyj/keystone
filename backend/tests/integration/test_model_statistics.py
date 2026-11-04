"""Real PostgreSQL analytics: exact costs, scope, facets and drill-down agree."""

from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from test_files_dictionary import client as client
from test_identity import bearer, create_user, query, token
from test_model_access import channel, entry
from test_model_access import model_configuration as model_configuration

from keystone.platform.model_access.models import ModelInvocation

pytestmark = pytest.mark.postgres
BOUNDS = {"since": "2026-10-18T00:00:00Z", "until": "2026-10-21T00:00:00Z"}


@pytest.fixture
def facts(client):
    admin = UUID(client.get("/api/v1/auth/me").json()["user"]["id"])
    other = UUID(create_user(client, "analyst")["id"])
    channels = [channel(client), channel(client, "newapi", api_base="https://stats.example/v1")]
    entries = [
        entry(client, owner, key=f"model_{i}", name=f"模型 {i}") for i, owner in enumerate(channels)
    ]
    run, agent = uuid4(), uuid4()
    rows = []

    async def insert(session):
        for i, (who, model, status, amount, unit, check, context, usage, timestamp) in enumerate(
            [
                (
                    admin,
                    0,
                    "failed",
                    "1.000000000000000001",
                    "credits",
                    "confirmed",
                    None,
                    {"input_tokens": 10, "output_tokens": 5},
                    "2026-10-19T16:05:00Z",
                ),
                (
                    admin,
                    0,
                    "succeeded",
                    "0",
                    "credits",
                    "confirmed",
                    None,
                    None,
                    "2026-10-19T23:00:00Z",
                ),
                (
                    other,
                    1,
                    "succeeded",
                    "0.123",
                    "USD",
                    "confirmed",
                    {
                        "flow_run_id": str(run),
                        "node_key": "summarize",
                        "model_role": "writer",
                        "agent_key": "planner",
                        "agent_call_id": str(agent),
                    },
                    {"input_tokens": 20, "output_tokens": 8},
                    "2026-10-20T01:00:00Z",
                ),
                (
                    None,
                    1,
                    "succeeded",
                    None,
                    None,
                    "pending",
                    {"flow_run_id": str(run), "node_key": "review"},
                    {"input_tokens": 0},
                    "2026-10-20T02:00:00Z",
                ),
                (
                    admin,
                    0,
                    "unknown",
                    None,
                    None,
                    "unavailable",
                    None,
                    {"input_tokens": "bad"},
                    "2026-10-18T16:00:00Z",
                ),
            ]
        ):
            created = datetime.fromisoformat(timestamp)
            row = ModelInvocation(
                id=uuid4(),
                user_id=who,
                channel_id=UUID(channels[model]["id"]),
                model_entry_id=UUID(entries[model]["id"]),
                provider_model=f"upstream-{model}",
                operation="image.generate" if i == 3 else "text.generate",
                status=status,
                cost_amount=Decimal(amount) if amount is not None else None,
                cost_unit=unit,
                cost_source="query" if amount is not None else "unknown",
                cost_check_status=check,
                created_at=created,
                finished_at=created + timedelta(seconds=i + 1),
                flow_context=context,
                usage=usage,
                error_code="model_rejected" if i == 0 else None,
            )
            session.add(row)
            rows.append(str(row.id))

    query(client, insert)
    return {
        "channels": channels,
        "entries": entries,
        "run": str(run),
        "other": str(other),
        "ids": rows,
    }


def statistics(client, params=None, headers=None):
    response = client.get(
        "/api/v1/model-invocations/statistics", params={**BOUNDS, **(params or {})}, headers=headers
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_exact_money_unknown_coverage_timezone_and_paginated_groups(client, facts):
    result = statistics(client, {"limit": 1})
    assert result["timezone"] == "Asia/Shanghai"
    summary = result["summary"]
    assert (
        summary["calls"] == 5 and summary["known_calls"] == 3 and summary["unknown_cost_calls"] == 2
    )
    assert summary["failed"] == 1 and summary["pending_calls"] == 1
    assert summary["input_tokens"] == "30" and summary["input_reported"] == 3
    assert summary["output_tokens"] == "13" and summary["output_reported"] == 2
    costs = {item["unit"]: item for item in summary["costs"]}
    assert costs["credits"]["amount"] == "1.000000000000000001"
    assert Decimal(costs["credits"]["avg_cost"]) == Decimal("0.500000000000000000")
    assert costs["USD"]["amount"] == "0.123000000000000000"
    assert result["total_groups"] == 2 and len(result["groups"]) == 1
    assert len(result["trend"]) == 2
    assert result["trend"][0]["label"].startswith("2026-10-19T00:00:00+08:00")
    second = statistics(client, {"offset": 1, "limit": 1})
    assert second["summary"] == summary and second["groups"][0]["key"] != result["groups"][0]["key"]
    for point in result["trend"]:
        calls = client.get("/api/v1/model-invocations", params=point["filters"]).json()
        assert len(calls) == point["calls"]


@pytest.mark.parametrize(
    "dimension",
    [
        "channel",
        "model",
        "upstream_model",
        "user",
        "source",
        "flow_run",
        "node",
        "model_role",
        "agent",
        "operation",
        "status",
        "cost_status",
        "cost_source",
        "error",
    ],
)
def test_every_dimension_drills_into_same_scoped_facts(client, facts, dimension):
    result = statistics(client, {"group_by": dimension})
    assert sum(row["calls"] for row in result["groups"]) == 5
    for row in result["groups"]:
        response = client.get("/api/v1/model-invocations", params={**BOUNDS, **row["filters"]})
        assert response.status_code == 200, response.text
        assert len(response.json()) == row["calls"]
        legacy = client.get(
            "/api/v1/model-invocations/summary", params={**BOUNDS, **row["filters"]}
        ).json()
        assert sum(legacy["attempts_by_status"].values()) == row["calls"]


def test_permission_filter_applies_to_all_aggregates_and_selector_names(client, facts):
    headers = bearer(token(client, ["model_invocation:read"])["token"])
    own = statistics(client, headers=headers)
    assert own["summary"]["calls"] == 3 and [cost["unit"] for cost in own["summary"]["costs"]] == [
        "credits"
    ]
    for dimension in ["user", "channel", "model", "flow_run", "node", "agent"]:
        response = client.get(
            "/api/v1/model-invocations/options",
            params={**BOUNDS, "dimension": dimension},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        assert (
            "analyst" not in response.text
            and facts["other"] not in response.text
            and facts["run"] not in response.text
        )
    assert statistics(client, {"user_id": facts["other"]}, headers)["summary"]["calls"] == 0
    forbidden = bearer(token(client, ["model:manage"])["token"])
    assert client.get("/api/v1/model-invocations/statistics", headers=forbidden).status_code == 403
    assert client.get("/api/v1/model-invocations/options", headers=forbidden).status_code == 403
    # Read-all without read cannot expose statistics either.
    forbidden = bearer(token(client, ["model_invocation:read_all"])["token"])
    assert client.get("/api/v1/model-invocations/statistics", headers=forbidden).status_code == 403


def test_scoped_searchable_facets_do_not_need_configuration_or_user_management(client, facts):
    headers = bearer(token(client, ["model_invocation:read", "model_invocation:read_all"])["token"])
    for dimension, q, expected in [
        ("model", "模型 1", "模型 1 (model_1)"),
        ("user", "analyst", "analyst"),
        ("node", "summ", "summarize"),
    ]:
        response = client.get(
            "/api/v1/model-invocations/options",
            params={**BOUNDS, "dimension": dimension, "q": q},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        assert [row["label"] for row in response.json()["items"]] == [expected]
        assert "api_key" not in response.text and "api_base" not in response.text
    first = client.get(
        "/api/v1/model-invocations/options",
        params={**BOUNDS, "dimension": "user", "limit": 1},
        headers=headers,
    ).json()
    second = client.get(
        "/api/v1/model-invocations/options",
        params={**BOUNDS, "dimension": "user", "offset": 1, "limit": 1},
        headers=headers,
    ).json()
    assert first["items"][0]["id"] == "system" and first["has_more"]
    assert second["items"][0]["id"] != "system"


@pytest.mark.parametrize(
    "params",
    [
        {"since": "2026-01-01T00:00:00Z", "until": "2026-10-21T00:00:00Z", "bucket": "hour"},
        {"since": "2020-01-01T00:00:00Z"},
    ],
)
def test_invalid_statistics_queries_are_bounded(client, params):
    assert (
        client.get("/api/v1/model-invocations/statistics", params={**BOUNDS, **params}).status_code
        == 422
    )


def test_empty_range_has_no_invented_money_and_cost_ranking_is_unit_specific(client, facts):
    empty = statistics(client, {"since": "2020-01-01T00:00:00Z", "until": "2020-01-02T00:00:00Z"})
    assert empty["summary"]["calls"] == 0 and empty["summary"]["costs"] == []
    assert empty["summary"]["input_tokens"] is None and empty["groups"] == []
    usd = statistics(client, {"order_by": "cost", "sort_unit": "USD", "limit": 1})
    credits = statistics(client, {"order_by": "cost", "sort_unit": "credits", "limit": 1})
    assert usd["groups"][0]["key"] == facts["channels"][1]["id"]
    assert credits["groups"][0]["key"] == facts["channels"][0]["id"]
    second = statistics(client, {"order_by": "cost", "sort_unit": "USD", "limit": 1, "offset": 1})
    assert second["groups"][0]["key"] == credits["groups"][0]["key"]


def test_cost_ranking_keeps_decimal_precision_beyond_default_context(client, facts):
    expected = max(row["id"] for row in facts["channels"])

    async def expensive(session):
        for index in (0, 2):
            row = await session.get(ModelInvocation, UUID(facts["ids"][index]))
            row.cost_unit = "credits"
            row.cost_amount = Decimal(
                "10000000000000000000.000000000000000002"
                if str(row.channel_id) == expected
                else "10000000000000000000.000000000000000001"
            )

    query(client, expensive)
    result = statistics(client, {"order_by": "cost", "sort_unit": "credits"})
    assert result["groups"][0]["key"] == expected
    assert result["summary"]["costs"][0]["amount"] == "20000000000000000000.000000000000000003"

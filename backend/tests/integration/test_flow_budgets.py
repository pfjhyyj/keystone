"""Native PostgreSQL budget contention, stable replay, input admission and settlement."""

import asyncio
from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from test_files_dictionary import client as client
from test_files_dictionary import upload
from test_identity import query

from keystone.core.errors import ServiceError
from keystone.core.schemas import Input
from keystone.platform.files.models import FileReference
from keystone.platform.flow.models import FlowBudgetClaim, FlowRun, FlowRunBudget
from keystone.platform.flow.schemas.budgets import FlowLimits
from keystone.platform.flow.schemas.definitions import FlowDefinition
from keystone.platform.flow.services.budgets import (
    FlowBudgetService,
    digest,
    reserve_in_transaction,
)
from keystone.platform.flow.services.identity import ExecutionIdentity
from keystone.platform.model_access.models import ModelInvocation
from keystone.platform.model_access.schemas.budgets import ModelRequestBudget

pytestmark = pytest.mark.postgres


class Empty(Input):
    pass


class Sources(Input):
    files: list[UUID]


BASE = FlowDefinition(
    key="test.budgets",
    name="Budget",
    description="Only tests",
    revision="1",
    input_model=Empty,
    output_model=Empty,
    entrypoint="test.budgets.run",
)


def accept(client, *, limits=None, sources=None, request_key="budget"):
    definition = replace(BASE, limits=limits or FlowLimits())
    if sources is not None:
        definition = replace(
            definition,
            input_model=Sources,
            file_fields=("files",),
            required_permissions=("file:content_read",),
        )
    runtime = client.app.state.runtime
    runtime.flow_definitions = {definition.key: definition}
    data = {
        "definition_key": definition.key,
        "request_key": request_key,
        "input": {"files": sources} if sources is not None else {},
    }
    response = client.post("/api/v1/flow-runs", json=data)
    return response, data


def identity(client, run_id):
    return ExecutionIdentity(client.app.state.runtime, UUID(str(run_id)))


def test_input_files_are_unique_budgeted_and_rejected_atomically(client):
    file_id = upload(client, b"1234").json()["id"]
    other = upload(client, b"5678", key="other").json()["id"]
    limits = FlowLimits(file_count=1, file_bytes=4)
    rejected, data = accept(client, limits=limits, sources=[file_id, other])
    assert rejected.status_code == 409 and rejected.json()["code"] == "flow_budget_exceeded"
    assert query(client, lambda s: s.scalar(select(FlowRun.id))) is None
    assert query(client, lambda s: s.scalar(select(FlowRunBudget.run_id))) is None
    assert query(client, lambda s: s.scalar(select(FileReference.file_id))) is None
    response = client.post(
        "/api/v1/flow-runs", json={**data, "input": {"files": [file_id, file_id]}}
    )
    assert response.status_code == 202, response.text
    run_id = UUID(response.json()["id"])
    budget = query(client, lambda s: s.get(FlowRunBudget, run_id))
    assert budget.counters["file_count"] == 1 and budget.counters["file_bytes"] == 4
    assert len(query(client, lambda s: s.scalars(select(FlowBudgetClaim))).all()) == 1
    # The accepted request key preserves its original declaration, even after current limits change.
    runtime = client.app.state.runtime
    runtime.flow_definitions = {
        BASE.key: replace(runtime.flow_definitions[BASE.key], limits=FlowLimits(file_count=0))
    }
    replay = client.post("/api/v1/flow-runs", json={**data, "input": {"files": [file_id, file_id]}})
    assert replay.json()["id"] == str(run_id)
    assert replay.json()["definition_snapshot"]["limits"]["file_count"] == 1


def test_concurrent_reservations_have_one_winner_and_stable_replay(client):
    response, _ = accept(client, limits=FlowLimits(tool_calls=1))
    assert response.status_code == 202, response.text
    run_id = UUID(response.json()["id"])
    access = identity(client, run_id)

    async def reserve(key, request_digest):
        async with access.access() as (session, _):
            return await reserve_in_transaction(
                session,
                run_id,
                kind="tool",
                key=key,
                amounts={"tool_calls": 1},
                request_digest=request_digest,
            )

    async def compete():
        return await asyncio.gather(
            reserve("first", digest(1)), reserve("second", digest(2)), return_exceptions=True
        )

    result = client.portal.call(compete)
    assert sum(item is True for item in result) == 1
    assert (
        sum(
            isinstance(item, ServiceError) and item.code == "flow_budget_exceeded"
            for item in result
        )
        == 1
    )
    winner = "first" if result[0] is True else "second"
    assert client.portal.call(reserve, winner, digest(1 if winner == "first" else 2)) is False
    with pytest.raises(ServiceError) as conflict:
        client.portal.call(reserve, winner, digest("different"))
    assert conflict.value.code == "flow_budget_conflict"
    budget = query(client, lambda s: s.get(FlowRunBudget, run_id))
    assert budget.counters["tool_calls"] == 1
    assert len(query(client, lambda s: s.scalars(select(FlowBudgetClaim))).all()) == 1


def test_budget_reservation_rolls_back_with_its_callers_transaction(client):
    response, _ = accept(client)
    run_id = UUID(response.json()["id"])
    access = identity(client, run_id)

    async def failed():
        async with access.access() as (session, _):
            await reserve_in_transaction(
                session,
                run_id,
                kind="iteration",
                key="one",
                amounts={"iterations": 1},
                request_digest=digest(1),
            )
            raise RuntimeError("rollback")

    with pytest.raises(RuntimeError):
        client.portal.call(failed)
    assert query(client, lambda s: s.get(FlowRunBudget, run_id)).counters["iterations"] == 0
    assert query(client, lambda s: s.scalar(select(FlowBudgetClaim.key))) is None


@pytest.mark.parametrize("reported", [None, 12])
@pytest.mark.parametrize("operation,output", [("text.generate", 16), ("text.embed", 0)])
def test_token_settlement_keeps_original_facts_and_does_not_refund_output(
    client, reported, operation, output
):
    response, _ = accept(client, limits=FlowLimits(input_tokens=10, output_tokens=16))
    run_id = UUID(response.json()["id"])
    budget = FlowBudgetService(identity(client, run_id))
    identifier = uuid4()
    request = ModelRequestBudget(operation=operation, max_output_tokens=output)

    async def reserve():
        async with budget.identity.access() as (session, _):
            await budget.reserve_model(session, identifier, request)

    client.portal.call(reserve)

    async def settle():
        async with budget.identity.database.transaction() as session:
            await budget.settle_model(
                session, identifier, {"input_tokens": reported} if reported is not None else None
            )

    client.portal.call(settle)
    client.portal.call(settle)
    current = query(client, lambda s: s.get(FlowRunBudget, run_id))
    assert current.counters["model_requests"] == 1 and current.counters["output_tokens"] == output
    assert current.counters["input_tokens"] == (reported or 0)
    assert current.counters["unreported_text_requests"] == int(reported is None)
    if reported is not None:
        with pytest.raises(ServiceError) as exceeded:
            client.portal.call(reserve)
        assert exceeded.value.code == "flow_budget_exceeded"

    async def conflicting():
        async with budget.identity.database.transaction() as session:
            await budget.settle_model(session, identifier, {"input_tokens": 1})

    with pytest.raises(ServiceError) as error:
        client.portal.call(conflicting)
    assert error.value.code == "flow_budget_conflict"
    assert query(client, lambda s: s.scalar(select(ModelInvocation.id))) is None


def test_database_constraints_reject_negative_budget_and_claim_values(client):
    from sqlalchemy import update
    from sqlalchemy.exc import IntegrityError

    response, _ = accept(client)
    run_id = UUID(response.json()["id"])
    access = identity(client, run_id)

    async def reserve():
        async with access.access() as (session, _):
            await reserve_in_transaction(
                session,
                run_id,
                kind="tool",
                key="one",
                amounts={"tool_calls": 1},
                request_digest=digest(1),
            )

    client.portal.call(reserve)
    for model, column in (
        (FlowRunBudget, "limits"),
        (FlowRunBudget, "counters"),
        (FlowBudgetClaim, "amounts"),
    ):

        async def corrupt(session, *, model=model, column=column):
            row = await session.scalar(select(model).where(model.run_id == run_id))
            values = {**getattr(row, column), "tool_calls": -1}
            await session.execute(
                update(model).where(model.run_id == run_id).values({column: values})
            )

        with pytest.raises(IntegrityError):
            query(client, corrupt)
    current = query(client, lambda s: s.get(FlowRunBudget, run_id))
    assert current.counters["tool_calls"] == 1 and current.limits["tool_calls"] == 256

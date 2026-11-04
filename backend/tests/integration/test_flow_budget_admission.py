"""Budget rejection and settlement use real SQL and SDK calls without a Worker."""

from uuid import UUID, uuid4

import httpx2
import pytest
from sqlalchemy import select
from support.budget_flow import (
    definition,
    disabled_definition,
    output_definition,
    tokens_definition,
)
from test_files_dictionary import client as client
from test_flow_budget_recovery import accept
from test_identity import query
from test_model_access import model_configuration as model_configuration

from keystone.core.errors import ServiceError
from keystone.core.worker_identity import WorkerIdentity
from keystone.platform.flow.models import FlowBudgetClaim, FlowRun, FlowRunBudget
from keystone.platform.flow.services.budgets import FlowBudgetService
from keystone.platform.flow.services.identity import ExecutionIdentity
from keystone.platform.model_access.models import ModelDispatchScope, ModelInvocation
from keystone.platform.model_access.schemas.invocations import (
    InvocationContext,
    Selection,
    TextRequest,
)
from keystone.platform.model_access.services.entries import ModelEntryService
from keystone.platform.model_access.services.runtime import ModelRuntime

pytestmark = pytest.mark.postgres


def model_for(client, run_id):
    runtime = client.app.state.runtime
    run = query(client, lambda s: s.get(FlowRun, UUID(run_id)))
    identity = ExecutionIdentity(runtime, run.id)
    budget = FlowBudgetService(identity)
    model = ModelRuntime(
        ModelEntryService(identity, runtime.models),
        admission=budget.reserve_model,
        settlement=budget.settle_model,
    )
    selected = Selection.model_validate(
        run.definition_snapshot["model_bindings"]["first"]["selection"]
    )
    context = InvocationContext(flow_run_id=run.id, node_key="call", model_role="first")
    owner = WorkerIdentity(runtime.settings.worker_id, uuid4(), runtime.execution_version())
    return model, selected, context, owner


@pytest.mark.parametrize(
    "case,contract,expected_calls",
    [
        ("input", tokens_definition, 1),
        ("output", output_definition, 1),
        ("disabled", disabled_definition, 0),
        ("usage", definition, 1),
    ],
)
def test_model_budget_stops_before_an_extra_send(
    client, monkeypatch, case, contract, expected_calls
):
    run_id = accept(client, monkeypatch, contract.key, 2, engine=False)
    model, selected, context, owner = model_for(client, run_id)
    sent = []
    original = model.clients.http.transport

    def transport(provider):
        underlying = original(provider)

        async def send(request):
            sent.append(request)
            response = await underlying.handle_async_request(request)
            if case == "usage":
                data = response.json()
                data["usage"].pop("prompt_tokens", None)
                return httpx2.Response(200, headers=response.headers, json=data)
            return response

        return httpx2.MockTransport(send)

    monkeypatch.setattr(model.clients.http, "transport", transport)

    async def call():
        return await model.invoke_text(
            TextRequest(messages=[{"role": "user", "content": "budget"}], max_tokens=16),
            selection=selected,
            invocation_id=uuid4(),
            context=context,
            worker_identity=owner,
        )

    for _ in range(expected_calls):
        client.portal.call(call)
    with pytest.raises(ServiceError) as error:
        client.portal.call(call)
    assert error.value.code == "flow_budget_exceeded"
    assert len(sent) == expected_calls
    rows = query(client, lambda s: s.scalars(select(ModelInvocation))).all()
    assert len(rows) == expected_calls and all(
        r.status == "succeeded" and r.cost_source == "response" for r in rows
    )
    budget = query(client, lambda s: s.get(FlowRunBudget, UUID(run_id)))
    assert budget.counters["model_requests"] == expected_calls
    assert budget.counters["output_tokens"] == expected_calls * 16
    assert budget.counters["input_tokens"] == (0 if case == "usage" else expected_calls * 12)
    assert budget.counters["unreported_text_requests"] == int(case == "usage")
    claims = query(client, lambda s: s.scalars(select(FlowBudgetClaim))).all()
    assert len([claim for claim in claims if claim.kind == "model"]) == expected_calls
    if not expected_calls:
        assert query(client, lambda s: s.scalar(select(ModelDispatchScope.scope))) is None

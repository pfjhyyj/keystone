"""Native steps share immutable Run allowances; the fixture has no production registration."""

from dataclasses import replace
from uuid import UUID

from pydantic import Field

from keystone.core.schemas import Input
from keystone.platform.flow.schemas.budgets import FlowLimits
from keystone.platform.flow.schemas.definitions import FlowDefinition, ModelRole
from keystone.platform.flow.schemas.packages import FlowPackage


class Request(Input):
    attempts: int = Field(default=2, strict=True, ge=1, le=3)
    max_tokens: int = Field(default=16, strict=True, ge=1, le=32)


class Result(Input):
    invocation_ids: list[UUID]


definition = FlowDefinition(
    key="test.budget_shared",
    name="Shared budget",
    description="Test only",
    revision="1",
    input_model=Request,
    output_model=Result,
    entrypoint="test.budget_shared.run",
    model_roles=(ModelRole("first", "First"), ModelRole("second", "Second")),
    required_permissions=("model:invoke",),
    limits=FlowLimits(model_requests=2, output_tokens=32, input_tokens=100),
)
tokens_definition = replace(
    definition,
    key="test.budget_tokens",
    entrypoint="test.budget_tokens.run",
    limits=FlowLimits(model_requests=2, output_tokens=32, input_tokens=10),
)
output_definition = replace(
    definition,
    key="test.budget_output",
    entrypoint="test.budget_output.run",
    limits=FlowLimits(model_requests=2, output_tokens=16, input_tokens=100),
)
disabled_definition = replace(
    definition,
    key="test.budget_disabled",
    entrypoint="test.budget_disabled.run",
    limits=FlowLimits(model_requests=0),
)


def after_checkpoint(run_id, index):
    """Test-only process exit boundary after a native Step has persisted its output."""


def register():
    from dbos import DBOS

    from keystone.platform.flow.workflows.context import flow_context
    from keystone.platform.model_access.schemas.invocations import TextRequest

    @DBOS.step(name="test.budget.input", retries_allowed=False)
    async def inputs(run_id):
        return await flow_context(run_id).inputs()

    @DBOS.step(name="test.budget.call", retries_allowed=False)
    async def call(run_id, index):
        ctx = flow_context(run_id)
        data = await ctx.inputs()
        result = await ctx.invoke_text(
            "first" if index % 2 == 0 else "second",
            TextRequest(
                messages=[{"role": "user", "content": "budget fixture"}],
                max_tokens=data["max_tokens"],
            ),
            node_key="call",
        )
        return str(result.invocation_id)

    def workflow(entrypoint):
        @DBOS.workflow(name=entrypoint)
        async def run(run_id):
            data = await inputs(run_id)
            result = []
            for index in range(data["attempts"]):
                result.append(await call(run_id, index))
                after_checkpoint(run_id, index)
            return {"invocation_ids": result}

        return run

    for item in PACKAGE.definitions:
        workflow(item.entrypoint)


PACKAGE = FlowPackage(
    key="budget-test",
    host_api_version=1,
    definitions=(definition, tokens_definition, output_definition, disabled_definition),
    execution_modules=("support.budget_flow",),
    execution_packages=("support",),
)

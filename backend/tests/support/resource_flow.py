"""Controlled tools, iterations, artifacts and independent Agents sharing one Run budget."""

from dataclasses import replace
from uuid import UUID

from pydantic import Field

from keystone.core.schemas import Input
from keystone.platform.flow.schemas.budgets import FlowLimits
from keystone.platform.flow.schemas.definitions import FlowDefinition, ModelRole
from keystone.platform.flow.schemas.packages import FlowPackage


class Request(Input):
    attempts: int = Field(default=2, strict=True, ge=1, le=2)


class Result(Input):
    done: int
    files: list[UUID] = Field(default_factory=list)


tool_definition = FlowDefinition(
    key="test.resources_tool",
    name="Resource tool",
    description="Test only",
    revision="1",
    input_model=Request,
    output_model=Result,
    entrypoint="test.resources_tool.run",
    limits=FlowLimits(tool_calls=1, iterations=8),
)
iteration_definition = replace(
    tool_definition,
    key="test.resources_iteration",
    entrypoint="test.resources_iteration.run",
    limits=FlowLimits(tool_calls=8, iterations=1),
)
artifact_definition = replace(
    tool_definition,
    key="test.resources_artifact",
    entrypoint="test.resources_artifact.run",
    required_permissions=("file:create", "file:content_read"),
    output_file_fields=("files",),
    limits=FlowLimits(file_count=1, file_bytes=4, temporary_bytes=8, concurrent_operations=1),
)
workspace_definition = replace(
    artifact_definition,
    key="test.resources_workspace",
    entrypoint="test.resources_workspace.run",
    limits=FlowLimits(file_count=1, file_bytes=4, temporary_bytes=4, concurrent_operations=1),
)
agent_definition = replace(
    tool_definition,
    key="test.resources_agents",
    entrypoint="test.resources_agents.run",
    required_permissions=("model:invoke",),
    model_roles=(ModelRole("review", "Review"),),
    limits=FlowLimits(tool_calls=1, iterations=8, model_requests=8),
)


def after_reserved(run_id, kind, index):
    """Fault injection after durable consumption and before native Step completion."""


def after_checkpoint(run_id, kind, index):
    """Fault injection after native checkpoints or the first durable Agent."""


def register():
    from uuid import uuid5

    from dbos import DBOS
    from pydantic_ai import Agent, RunContext, ToolOutput
    from pydantic_ai.capabilities import ResolveModelId
    from pydantic_ai.durable_exec.dbos import DBOSDurability
    from pydantic_ai.usage import UsageLimits

    from keystone.examples.document_review.contracts import Judgment
    from keystone.platform.flow.services.budgets import digest
    from keystone.platform.flow.workflows.agent_model import resolve_model
    from keystone.platform.flow.workflows.context import flow_context

    @DBOS.step(name="test.resources.inputs", retries_allowed=False)
    async def inputs(run_id):
        return await flow_context(run_id).inputs()

    @DBOS.step(name="test.resources.tool", retries_allowed=False)
    async def tool(run_id, index):
        await flow_context(run_id).consume_tool("tool", request_digest=digest([index]))
        after_reserved(run_id, "tool", index)
        return index

    @DBOS.step(name="test.resources.iteration", retries_allowed=False)
    async def iteration(run_id, index):
        await flow_context(run_id).consume_iteration("candidate")
        after_reserved(run_id, "iteration", index)
        return index

    @DBOS.step(name="test.resources.artifact", retries_allowed=False)
    async def artifact(run_id, index):
        result = await flow_context(run_id).upload(
            f"output.{index}", "output.txt", b"data", content_type="text/plain"
        )
        return str(result)

    def workflow(kind, entrypoint, step):
        @DBOS.workflow(name=entrypoint)
        async def run(run_id):
            data = await inputs(run_id)
            files = []
            for index in range(data["attempts"]):
                result = await step(run_id, index)
                if kind == "artifact":
                    files.append(result)
                after_checkpoint(run_id, kind, index)
            return {"done": data["attempts"], "files": files}

        return run

    workflow("tool", tool_definition.entrypoint, tool)
    workflow("iteration", iteration_definition.entrypoint, iteration)
    workflow("artifact", artifact_definition.entrypoint, artifact)

    @DBOS.step(name="test.resources.workspace", retries_allowed=False)
    async def working_file(run_id):
        ctx = flow_context(run_id)
        async with ctx.workspace(max_bytes=4) as path:
            path.write_bytes(b"data")
            after_reserved(run_id, "workspace", 0)
            identifier = await ctx.upload(
                "copy", "copy.txt", path.read_bytes(), content_type="text/plain"
            )
            return str(identifier)

    @DBOS.workflow(name=workspace_definition.entrypoint)
    async def workspace_run(run_id):
        return {"done": 1, "files": [await working_file(run_id)]}

    @DBOS.step(name="test.resources.agent_tool", retries_allowed=False)
    async def inspect(run_id):
        await flow_context(run_id).consume_tool("inspect_document", request_digest=digest([run_id]))
        return {"source_file_id": run_id, "characters": 0}

    async def inspect_document(ctx: RunContext[dict]):
        return await inspect(ctx.deps["run_id"])

    def agent(name):
        return Agent(
            "keystone:flow",
            name="test.resources." + name,
            defer_model_check=True,
            deps_type=dict,
            output_type=ToolOutput(Judgment, name="submit_review"),
            tools=[inspect_document],
            retries=0,
            capabilities=[
                ResolveModelId(resolve_model),
                DBOSDurability(
                    parallel_execution_mode="sequential",
                    model_step_config={"retries_allowed": False},
                ),
            ],
        )

    agents = (agent("first"), agent("second"))

    @DBOS.workflow(name=agent_definition.entrypoint)
    async def run_agents(run_id):
        for index, current in enumerate(agents):
            await current.run(
                "Inspect then submit",
                deps={
                    "run_id": str(run_id),
                    "model_role": "review",
                    "node_key": f"agent{index}",
                    "agent_key": f"agent{index}",
                    "agent_call_id": str(uuid5(UUID(str(run_id)), f"agent:{index}")),
                },
                model_settings={"max_tokens": 64},
                usage_limits=UsageLimits(request_limit=2, tool_calls_limit=1),
            )
            after_checkpoint(run_id, "agents", index)
        return {"done": 2, "files": []}


PACKAGE = FlowPackage(
    key="resource-test",
    host_api_version=1,
    definitions=(
        tool_definition,
        iteration_definition,
        artifact_definition,
        agent_definition,
        workspace_definition,
    ),
    execution_modules=("support.resource_flow",),
    execution_packages=("support",),
)

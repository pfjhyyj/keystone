"""Typed model operations fixture; native steps and explicit package only."""

from uuid import UUID

from keystone.core.schemas import Input
from keystone.platform.flow.schemas.definitions import FlowDefinition, ModelRole
from keystone.platform.flow.schemas.packages import FlowPackage
from keystone.platform.model_access.schemas.images import (
    ImageEditRequest,
    ImageGenerateRequest,
    ImageResult,
    ImageTaskRequest,
)


class Question(Input):
    prompt: str


class Answer(Input):
    answer: int
    invocation_id: UUID


definition = FlowDefinition(
    key="test.structured",
    name="Structured fixture",
    description="Test only",
    revision="1",
    input_model=Question,
    output_model=Answer,
    entrypoint="test.structured.run",
    required_permissions=("model:invoke",),
    model_roles=(ModelRole("analyze", "Analysis", "text.structured"),),
)


def register():
    from dbos import DBOS

    from keystone.platform.flow.workflows.context import flow_context
    from keystone.platform.model_access.schemas.structured import JsonOutput, StructuredRequest

    @DBOS.step(name="test.structured.answer", retries_allowed=False)
    async def answer(run_id):
        ctx = flow_context(run_id)
        inputs = await ctx.inputs()
        result = await ctx.invoke_structured(
            "analyze",
            StructuredRequest(
                messages=[{"role": "user", "content": inputs["prompt"]}],
                output=JsonOutput(
                    name="Answer",
                    json_schema={
                        "type": "object",
                        "properties": {"answer": {"type": "integer"}},
                        "required": ["answer"],
                        "additionalProperties": False,
                    },
                ),
            ),
            node_key="analyze",
        )
        return {**result.content, "invocation_id": str(result.invocation_id)}

    @DBOS.workflow(name=definition.entrypoint)
    async def run(run_id):
        return await answer(run_id)

    @DBOS.step(name="test.vision.answer", retries_allowed=False)
    async def vision_answer(run_id):
        from keystone.platform.model_access.schemas.images import ImageInput, VisionRequest

        ctx = flow_context(run_id)
        inputs = await ctx.inputs()
        result = await ctx.invoke_vision(
            "analyze",
            VisionRequest(
                messages=[{"role": "user", "content": inputs["prompt"]}],
                images=[
                    ImageInput(file_id=inputs.get("image_file_id") or inputs["source_file_id"])
                ],
            ),
            node_key="analyze",
        )
        return result.model_dump(mode="json")

    @DBOS.workflow(name=vision_definition.entrypoint)
    async def vision(run_id):
        return await vision_answer(run_id)

    @DBOS.step(name="test.image.generate", retries_allowed=False)
    async def generate_image(run_id):
        ctx = flow_context(run_id)
        result = await ctx.invoke_image(
            "generate",
            ImageGenerateRequest.model_validate(await ctx.inputs()),
            node_key="generate",
        )
        after_images(run_id, result)
        return result.model_dump(mode="json")

    @DBOS.workflow(name=image_definition.entrypoint)
    async def image_run(run_id):
        return await generate_image(run_id)

    @DBOS.step(name="test.image.edit", retries_allowed=False)
    async def edit_image(run_id):
        ctx = flow_context(run_id)
        result = await ctx.edit_image(
            "edit",
            ImageEditRequest.model_validate(await ctx.inputs()),
            node_key="edit",
        )
        after_images(run_id, result)
        return result.model_dump(mode="json")

    @DBOS.workflow(name=edit_definition.entrypoint)
    async def edit_run(run_id):
        return await edit_image(run_id)

    @DBOS.step(name="test.image_task.submit", retries_allowed=False)
    async def submit_task(run_id):
        ctx = flow_context(run_id)
        result = await ctx.submit_image(
            "generate",
            ImageTaskRequest.model_validate(await ctx.inputs()),
            node_key="generate",
        )
        return result.model_dump(mode="json")

    @DBOS.step(name="test.image_task.result", retries_allowed=False)
    async def task_result(run_id, identifier):
        result = await flow_context(run_id).image_result(
            "generate", identifier, node_key="generate"
        )
        return result.model_dump(mode="json")

    @DBOS.workflow(name=task_definition.entrypoint)
    async def task_run(run_id):
        from keystone.platform.model_access.schemas.images import ImagePending

        task = await submit_task(run_id)
        for _ in range(60):
            result = await task_result(run_id, task["invocation_id"])
            if result.get("state") != "pending":
                return result
            pending = ImagePending.model_validate(result)
            await DBOS.sleep_async(pending.retry_after_seconds)
        raise RuntimeError("model_task_timeout")

    from uuid import uuid5

    from pydantic_ai import Agent, NativeOutput
    from pydantic_ai.capabilities import ResolveModelId
    from pydantic_ai.durable_exec.dbos import DBOSDurability

    from keystone.platform.flow.workflows.agent_model import resolve_model

    agent = Agent(
        "keystone:flow",
        name="test.native.structured_agent",
        defer_model_check=True,
        deps_type=dict,
        output_type=NativeOutput(AgentAnswer, strict=True),
        retries=0,
        capabilities=[
            ResolveModelId(resolve_model),
            DBOSDurability(
                parallel_execution_mode="sequential",
                model_step_config={"retries_allowed": False},
            ),
        ],
    )

    @DBOS.step(name="test.structured_agent.input", retries_allowed=False)
    async def agent_input(run_id):
        return await flow_context(run_id).inputs()

    @DBOS.workflow(name=agent_definition.entrypoint)
    async def agent_run(run_id):
        data = await agent_input(run_id)
        result = await agent.run(
            data["prompt"],
            deps={
                "run_id": str(run_id),
                "model_role": "analyze",
                "node_key": "analyze",
                "agent_key": "native",
                "agent_call_id": str(uuid5(UUID(str(run_id)), "native:0")),
            },
        )
        return result.output.model_dump(mode="json")


class ImageQuestion(Input):
    prompt: str
    source_file_id: UUID
    image_file_id: UUID | None = None


class ImageAnswer(Input):
    content: str
    invocation_id: UUID


vision_definition = FlowDefinition(
    key="test.vision",
    name="Vision fixture",
    description="Test only",
    revision="1",
    input_model=ImageQuestion,
    output_model=ImageAnswer,
    entrypoint="test.vision.run",
    required_permissions=("model:invoke", "file:content_read"),
    file_fields=("source_file_id",),
    model_roles=(ModelRole("analyze", "Analysis", "image.understand"),),
)


def after_images(run_id, result):
    """Fault boundary: Files are durable; the native step has not checkpointed yet."""


image_definition = FlowDefinition(
    key="test.image",
    name="Image fixture",
    description="Test only",
    revision="1",
    input_model=ImageGenerateRequest,
    output_model=ImageResult,
    entrypoint="test.image.run",
    required_permissions=("model:invoke", "file:create", "file:content_read"),
    output_file_fields=("files.*.file_id",),
    model_roles=(ModelRole("generate", "Generate", "image.generate"),),
)
edit_definition = FlowDefinition(
    key="test.image_edit",
    name="Image edit fixture",
    description="Test only",
    revision="1",
    input_model=ImageEditRequest,
    output_model=ImageResult,
    entrypoint="test.image_edit.run",
    required_permissions=("model:invoke", "file:create", "file:content_read"),
    file_fields=("images.*.file_id",),
    output_file_fields=("files.*.file_id",),
    model_roles=(ModelRole("edit", "Edit", "image.edit"),),
)


task_definition = FlowDefinition(
    key="test.image_task",
    name="Image task fixture",
    description="Test only",
    revision="1",
    input_model=ImageTaskRequest,
    output_model=ImageResult,
    entrypoint="test.image_task.run",
    required_permissions=("model:invoke", "file:create", "file:content_read"),
    output_file_fields=("files.*.file_id",),
    model_roles=(ModelRole("generate", "Generate", "image.generate", delivery="async"),),
)


class AgentAnswer(Input):
    answer: int


agent_definition = FlowDefinition(
    key="test.structured_agent",
    name="Native structured Agent",
    description="Test only",
    revision="1",
    input_model=Question,
    output_model=AgentAnswer,
    entrypoint="test.structured_agent.run",
    required_permissions=("model:invoke",),
    model_roles=(ModelRole("analyze", "Analysis", "text.structured"),),
)


PACKAGE = FlowPackage(
    key="operation-test",
    host_api_version=1,
    definitions=(
        definition,
        vision_definition,
        image_definition,
        edit_definition,
        task_definition,
        agent_definition,
    ),
    execution_modules=("support.operation_flow",),
    execution_packages=("support",),
)

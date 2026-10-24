"""Test-only multi-role/collection workflow through real SDK and file services."""

from uuid import UUID

from pydantic import Field

from keystone.core.schemas import Input
from keystone.platform.flow.schemas.definitions import FlowDefinition, ModelRole
from keystone.platform.flow.schemas.packages import FlowPackage


class Document(Input):
    file_id: UUID


class Sources(Input):
    documents: list[Document] = Field(min_length=1, max_length=5)


class Reports(Input):
    reports: list[Document]


definition = FlowDefinition(
    key="test.multi_role",
    revision="1",
    name="Multiple role fixture",
    description="Test only",
    input_model=Sources,
    output_model=Reports,
    entrypoint="test.multi_role.run",
    required_permissions=("model:invoke", "file:create", "file:content_read"),
    model_roles=(ModelRole("analyze", "分析"), ModelRole("review", "复核")),
    file_fields=("documents.*.file_id",),
    output_file_fields=("reports.*.file_id",),
)
PACKAGE = FlowPackage(
    key="multi-role-test",
    host_api_version=1,
    definitions=(definition,),
    execution_modules=("support.multi_role_flow",),
    execution_packages=("support",),
)


def after_generate(run_id, role, result):
    """Fault-injection boundary after the durable model step, before output I/O."""


def register():
    from dbos import DBOS

    from keystone.platform.flow.workflows.context import flow_context
    from keystone.platform.model_access.schemas.invocations import Message, TextRequest

    @DBOS.step(name="test.multi_role.generate", retries_allowed=False)
    async def generate(run_id, role):
        ctx = flow_context(run_id)
        inputs = await ctx.inputs()
        content = await ctx.read_file(inputs["documents"][0]["file_id"], max_bytes=4096)
        result = await ctx.invoke_text(
            role,
            TextRequest(messages=[Message(role="user", content=content.decode())]),
            node_key=role,
        )
        return result.model_dump(mode="json")

    @DBOS.step(name="test.multi_role.save", retries_allowed=False)
    async def save(run_id, role, content):
        identifier = await flow_context(run_id).upload(
            role, f"{role}.txt", content.encode(), content_type="text/plain"
        )
        return {"file_id": str(identifier)}

    @DBOS.workflow(name=definition.entrypoint)
    async def run(run_id):
        reports = []
        for role in ("analyze", "review"):
            result = await generate(run_id, role)
            after_generate(run_id, role, result)
            reports.append(await save(run_id, role, result["content"]))
        return {"reports": reports}

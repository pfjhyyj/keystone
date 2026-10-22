from uuid import UUID, uuid5

from dbos import DBOS
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from keystone.core.errors import ServiceError
from keystone.platform.flow import host
from keystone.platform.model_access.schemas import FlowContext, Selection
from keystone.platform.model_access.services.entries import ModelEntryService
from keystone.platform.model_access.services.runtime import ModelRuntime


class FlowModel(Model):
    """Pydantic AI adapter to the existing controlled SDK, resolved inside each DBOS step."""

    model_name = "keystone-flow"
    system = "keystone"

    def __init__(self, deps):
        super().__init__()
        self.deps = deps

    async def request(self, messages, model_settings, model_request_parameters):
        if DBOS.workflow_id is None or DBOS.step_id is None:
            raise RuntimeError("Agent requests require a durable DBOS step")
        settings = {"max_tokens": 1024, **(model_settings or {})}
        limit = settings["max_tokens"]
        if type(limit) is not int or not 1 <= limit <= 4096:
            raise ServiceError(422, "model_invalid_request", "模型输出上限须为 1～4096 token")
        run_id = self.deps["run_id"]
        inputs = await host.inputs(run_id)
        selected = Selection.model_validate(inputs[self.deps["model_field"]])
        invocation_id = uuid5(UUID(run_id), f"model:{DBOS.workflow_id}:{DBOS.step_id}")
        service = ModelEntryService(host.identity(run_id), host.runtime().models)

        async def request(sdk):
            model = OpenAIChatModel(
                selected.provider_model, provider=OpenAIProvider(openai_client=sdk)
            )
            return await model.request(messages, settings, model_request_parameters)

        return await ModelRuntime(service).invoke_sdk(
            invocation_id=invocation_id,
            selection=selected,
            flow_context=FlowContext(
                flow_run_id=UUID(run_id),
                node_key=self.deps["node_key"],
                agent_key=self.deps["agent_key"],
                agent_call_id=UUID(self.deps["agent_call_id"]),
            ),
            sdk_request=request,
            worker_identity=host.runtime().worker_identity,
        )


def resolve_model(ctx, model_id):
    if model_id != "keystone:flow":
        raise ValueError("Only the platform model is available")
    return FlowModel(ctx.deps)

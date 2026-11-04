from uuid import UUID

from dbos import DBOS
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from keystone.core.errors import ServiceError
from keystone.platform.flow.workflows import context as host


class FlowModel(Model):
    """Pydantic AI adapter to the existing controlled SDK, resolved inside each DBOS step."""

    model_name = "keystone-flow"
    system = "keystone"

    def __init__(self, deps):
        super().__init__(profile={"supports_json_schema_output": True})
        self.deps = deps

    async def request(self, messages, model_settings, model_request_parameters):
        if DBOS.workflow_id is None or DBOS.step_id is None:
            raise RuntimeError("Agent requests require a durable DBOS step")
        settings = {"max_tokens": 1024, **(model_settings or {})}
        limit = settings["max_tokens"]
        if type(limit) is not int or not 1 <= limit <= 4096:
            raise ServiceError(422, "model_invalid_request", "模型输出上限须为 1～4096 token")
        run_id = self.deps["run_id"]
        ctx = host.flow_context(run_id)
        operation = (
            "text.structured"
            if model_request_parameters.output_mode == "native"
            else "text.generate"
        )
        runtime, selected, invocation_id = await ctx._model(
            self.deps["model_role"], operation=operation
        )
        settings = runtime.sdk_settings(selected, settings)

        async def request(sdk):
            model = OpenAIChatModel(
                selected.provider_model, provider=OpenAIProvider(openai_client=sdk)
            )
            return await model.request(messages, settings, model_request_parameters)

        return await runtime.invoke_sdk(
            invocation_id=invocation_id,
            selection=selected,
            flow_context=ctx._invocation_context(
                self.deps["model_role"],
                node_key=self.deps["node_key"],
                agent_key=self.deps["agent_key"],
                agent_call_id=UUID(self.deps["agent_call_id"]),
            ),
            sdk_request=request,
            worker_identity=host._runtime().worker_identity,
        )


def resolve_model(ctx, model_id):
    if model_id != "keystone:flow":
        raise ValueError("Only the platform model is available")
    return FlowModel(ctx.deps)

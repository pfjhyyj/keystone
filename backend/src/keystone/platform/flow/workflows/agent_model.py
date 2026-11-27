from uuid import UUID

from dbos import DBOS
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from keystone.core.errors import ServiceError
from keystone.core.time import utcnow
from keystone.platform.flow.workflows import context as host
from keystone.platform.flow.workflows.diagnostics import record_model_call
from keystone.platform.model_access.schemas.invocations import MAX_OUTPUT_TOKENS


def _safe_content(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_safe_content(item) for item in value[:32]]
    if isinstance(value, dict):
        return {str(key): _safe_content(item) for key, item in list(value.items())[:32]}
    return str(value)


def _project_part(part):
    kind = getattr(part, "part_kind", type(part).__name__)
    if kind in {"system-prompt", "user-prompt", "text", "thinking", "retry-prompt"}:
        return {"kind": kind, "content": _safe_content(getattr(part, "content", ""))}
    if kind == "tool-call":
        return {
            "kind": kind,
            "tool": getattr(part, "tool_name", ""),
            "args": _safe_content(getattr(part, "args", None)),
        }
    if kind == "tool-return":
        return {
            "kind": kind,
            "tool": getattr(part, "tool_name", ""),
            "content": _safe_content(getattr(part, "content", None)),
        }
    return {"kind": str(kind)}


def _project_messages(messages, max_tokens):
    items = []
    for message in messages[-24:]:
        parts = [_project_part(part) for part in getattr(message, "parts", ())]
        items.append({"role": getattr(message, "kind", "message"), "parts": parts})
    return {"messages": items, "max_tokens": max_tokens}


def _project_response(response):
    parts = [_project_part(part) for part in getattr(response, "parts", ())]
    return {"parts": parts}


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
        if type(limit) is not int or not 1 <= limit <= MAX_OUTPUT_TOKENS:
            raise ServiceError(
                422, "model_invalid_request", f"模型输出上限须为 1～{MAX_OUTPUT_TOKENS} token"
            )
        run_id = self.deps["run_id"]
        ctx = host.flow_context(run_id)
        operation = (
            "text.structured"
            if model_request_parameters.output_mode == "native"
            else "text.generate"
        )
        runtime, selected, invocation_id, definition_key = await ctx._model(
            self.deps["model_role"], operation=operation
        )
        from pydantic import ValidationError

        from keystone.platform.model_access.schemas.invocations import ReasoningOptions

        try:
            raw = settings.pop("keystone_reasoning", None)
            reasoning = ReasoningOptions.model_validate(raw) if raw is not None else None
        except ValidationError:
            raise ServiceError(422, "model_invalid_request", "思考设置无效") from None
        settings = runtime.sdk_settings(selected, settings, reasoning=reasoning)

        async def request(sdk):
            model = OpenAIChatModel(
                selected.provider_model, provider=OpenAIProvider(openai_client=sdk)
            )
            return await model.request(messages, settings, model_request_parameters)

        started_at = utcnow()
        input_data = _project_messages(messages, limit)
        record_enabled = self.deps.get("record_diagnostics", True)
        try:
            response = await runtime.invoke_sdk(
                invocation_id=invocation_id,
                selection=selected,
                flow_context=ctx._invocation_context(
                    self.deps["model_role"],
                    definition_key=definition_key,
                    node_key=self.deps["node_key"],
                    agent_key=self.deps["agent_key"],
                    agent_call_id=UUID(self.deps["agent_call_id"]),
                ),
                sdk_request=request,
                reasoning=reasoning,
                output_tokens=limit,
                worker_identity=host._runtime().worker_identity,
            )
        except Exception as error:
            await record_model_call(
                run_id,
                operation=operation,
                model_role=self.deps["model_role"],
                node_key=self.deps["node_key"],
                invocation_id=invocation_id,
                input_data=input_data,
                error=error,
                started_at=started_at,
                finished_at=utcnow(),
                enabled=record_enabled,
            )
            raise
        await record_model_call(
            run_id,
            operation=operation,
            model_role=self.deps["model_role"],
            node_key=self.deps["node_key"],
            invocation_id=invocation_id,
            input_data=input_data,
            output_data=_project_response(response),
            started_at=started_at,
            finished_at=utcnow(),
            enabled=record_enabled,
        )
        return response


def resolve_model(ctx, model_id):
    if model_id != "keystone:flow":
        raise ValueError("Only the platform model is available")
    return FlowModel(ctx.deps)

"""Business acceptance, real Agent correction, and bounded tool readback."""

from inspect import unwrap
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from document_review import workflow
from document_review.contracts import EXCERPT_CHARACTERS, PREVIEW_CHARACTERS
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import ModelResponse, RetryPromptPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from keystone.core.errors import ServiceError


@pytest.fixture(autouse=True)
def mock_workflow_context(monkeypatch):
    context = SimpleNamespace(
        inputs=AsyncMock(),
        read_file=AsyncMock(),
        upload=AsyncMock(),
    )
    monkeypatch.setattr(workflow, "flow_context", lambda _: context)
    return context


@pytest.fixture
def source():
    return {"source_file_id": str(uuid4()), "characters": 5}


def judgment(source, **overrides):
    return {**source, "verdict": "reviewed", "summary": "Evidence reviewed.", **overrides}


@pytest.mark.parametrize(
    "change",
    [
        {"source_file_id": str(uuid4())},
        {"characters": 6},
        {"summary": " \n\t "},
    ],
)
async def test_reject_before_any_report_upload(monkeypatch, source, change):
    upload = AsyncMock()
    monkeypatch.setattr(workflow.flow_context(None), "upload", upload)
    with pytest.raises(ServiceError) as caught:
        await unwrap(workflow.save_report)(str(uuid4()), judgment(source, **change), source)
    assert caught.value.code == "flow_invalid_output"
    upload.assert_not_called()


async def test_acceptance_saves_normalized_result_with_stable_artifact_key(monkeypatch, source):
    file_id, run_id = str(uuid4()), str(uuid4())
    upload = AsyncMock(return_value=file_id)
    monkeypatch.setattr(workflow.flow_context(None), "upload", upload)
    result = await unwrap(workflow.save_report)(
        run_id, judgment(source, summary="  Checked.  "), source
    )
    assert result == {**judgment(source, summary="Checked."), "report_file_id": file_id}
    assert upload.call_args.args[:2] == ("report", "review-report.txt")
    assert upload.call_args.args[2] == b"reviewed\n\nChecked.\n"


async def test_real_agent_repairs_source_mismatch(source):
    requests = []

    def respond(messages, info):
        requests.append(messages)
        wrong = {"characters": 6} if len(requests) == 1 else {}
        return ModelResponse([ToolCallPart("submit_review", judgment(source, **wrong))])

    with workflow.agent.override(model=FunctionModel(respond)):
        result = await workflow.review(str(uuid4()), source)
    assert result == judgment(source)
    assert len(requests) == 2
    feedback = [p for m in requests[1] for p in m.parts if isinstance(p, RetryPromptPart)]
    assert any("characters must match" in str(p.content) for p in feedback)


async def test_repeated_invalid_output_fails_workflow_without_publishing(monkeypatch, source):
    attempts = []
    run_id = str(uuid4())
    monkeypatch.setattr(workflow, "prepare", AsyncMock(return_value=source))
    inspected = AsyncMock(
        return_value={
            **source,
            "status": "succeeded",
            "summary": "Source",
            "preview": "hello",
            "truncated": False,
        }
    )
    monkeypatch.setattr(workflow, "inspect_source", inspected)
    save = AsyncMock()
    monkeypatch.setattr(workflow, "save_report", save)

    def respond(messages, info):
        attempts.append(1)
        if len(attempts) == 1:
            return ModelResponse([ToolCallPart("inspect_document", {})])
        return ModelResponse([ToolCallPart("submit_review", judgment(source, characters=6))])

    with workflow.agent.override(model=FunctionModel(respond)):
        with pytest.raises(RuntimeError) as caught:
            await unwrap(workflow.run)(run_id)
    assert str(caught.value) == "flow_invalid_output"
    assert len(attempts) == 3
    inspected.assert_awaited_once_with(run_id)
    save.assert_not_called()


async def test_tools_return_bounded_unicode_preview_and_exact_excerpt(monkeypatch):
    source_id, run_id = str(uuid4()), str(uuid4())
    text = "汉🙂" * 1600
    monkeypatch.setattr(
        workflow.flow_context(None), "inputs", AsyncMock(return_value={"source_file_id": source_id})
    )
    read = AsyncMock(return_value=text.encode())
    monkeypatch.setattr(workflow.flow_context(None), "read_file", read)
    summary = await unwrap(workflow.inspect_source)(run_id)
    assert "text" not in summary and summary["preview"] == text[:PREVIEW_CHARACTERS]
    assert summary["truncated"] and summary["characters"] == len(text)
    assert summary["source_file_id"] == source_id
    excerpt = await unwrap(workflow.read_excerpt)(run_id, PREVIEW_CHARACTERS, EXCERPT_CHARACTERS)
    assert excerpt["text"] == text[PREVIEW_CHARACTERS : PREVIEW_CHARACTERS + EXCERPT_CHARACTERS]
    assert excerpt["next_offset"] == PREVIEW_CHARACTERS + EXCERPT_CHARACTERS
    assert excerpt["truncated"]
    tail = await unwrap(workflow.read_excerpt)(run_id, len(text), 1)
    assert tail["text"] == "" and not tail["truncated"]
    assert all(call.kwargs == {"max_bytes": 16384} for call in read.call_args_list)


@pytest.mark.parametrize("offset,length", [(-1, 1), (0, 2049), (True, 1), (0, 0)])
async def test_invalid_tool_ranges_do_not_read_files(monkeypatch, offset, length):
    read = AsyncMock()
    monkeypatch.setattr(workflow.flow_context(None), "read_file", read)
    with pytest.raises(ValueError):
        await unwrap(workflow.read_excerpt)(str(uuid4()), offset, length)
    read.assert_not_called()


@pytest.mark.parametrize(
    "error,code",
    [
        (TimeoutError("private"), "flow_timeout"),
        (UnexpectedModelBehavior("private"), "flow_invalid_output"),
        (ServiceError(403, "execution_not_authorized", "private"), "execution_not_authorized"),
    ],
)
async def test_failure_stops_before_publication_and_exposes_only_safe_code(
    monkeypatch, source, error, code
):
    monkeypatch.setattr(workflow, "prepare", AsyncMock(return_value=source))
    monkeypatch.setattr(workflow, "review", AsyncMock(side_effect=error))
    save = AsyncMock()
    monkeypatch.setattr(workflow, "save_report", save)
    with pytest.raises(RuntimeError) as caught:
        await unwrap(workflow.run)(str(uuid4()))
    assert str(caught.value) == code
    save.assert_not_called()


async def test_model_output_limit_is_passed_to_sdk_callback(monkeypatch):
    from pydantic_ai.models import ModelRequestParameters

    from keystone.platform.flow.workflows import agent_model

    run_id = str(uuid4())
    request = AsyncMock(return_value="accepted")
    monkeypatch.setattr(agent_model, "DBOS", SimpleNamespace(workflow_id="flow:test", step_id=1))

    invocations = []

    async def invoke(**kwargs):
        invocations.append(kwargs)
        return await kwargs["sdk_request"](None)

    selected = SimpleNamespace(provider_model="test-model")
    prepare_settings = Mock(
        side_effect=lambda selected, settings, **kwargs: {**settings, "temperature": 0.25}
    )
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def scope():
        yield

    from keystone.platform.model_access.schemas.invocations import InvocationContext

    def correlation(role, **fields):
        return InvocationContext(
            flow_run_id=run_id, model_role=role, workflow_id="flow:test", step_id=1, **fields
        )

    context = SimpleNamespace(
        _invocation_context=Mock(side_effect=correlation),
        execution_scope=scope,
        _model=AsyncMock(
            return_value=(
                SimpleNamespace(invoke_sdk=invoke, sdk_settings=prepare_settings),
                selected,
                uuid4(),
            )
        ),
    )
    monkeypatch.setattr(agent_model.host, "flow_context", lambda _: context)
    monkeypatch.setattr(agent_model.host, "_runtime", lambda: SimpleNamespace(worker_identity=None))
    monkeypatch.setattr(agent_model, "OpenAIProvider", lambda **_: None)
    monkeypatch.setattr(
        agent_model, "OpenAIChatModel", lambda *_, **__: SimpleNamespace(request=request)
    )
    model = agent_model.FlowModel(
        {
            "run_id": run_id,
            "model_role": "review",
            "node_key": "review",
            "agent_key": "review_agent",
            "agent_call_id": str(uuid4()),
        }
    )
    for settings, expected in [
        (None, 1024),
        ({"max_tokens": 2048}, 2048),
        ({"max_tokens": 4096}, 4096),
    ]:
        assert await model.request([], settings, ModelRequestParameters()) == "accepted"
        assert request.call_args.args[1]["max_tokens"] == expected
        assert request.call_args.args[1]["temperature"] == 0.25
        assert prepare_settings.call_args.args[0] is selected
    await model.request(
        [], {"keystone_reasoning": {"mode": "on", "effort": "high"}}, ModelRequestParameters()
    )
    assert "keystone_reasoning" not in request.call_args.args[1]
    assert prepare_settings.call_args.kwargs["reasoning"].effort == "high"
    assert invocations[-1]["reasoning"] is prepare_settings.call_args.kwargs["reasoning"]
    before = request.await_count
    for invalid in [None, True, 0, 4097, "2048"]:
        with pytest.raises(ServiceError) as caught:
            await model.request([], {"max_tokens": invalid}, ModelRequestParameters())
        assert caught.value.code == "model_invalid_request"
    assert request.await_count == before
    assert prepare_settings.call_count == 4


def test_contract_import_does_not_load_agent_or_skill_runtime():
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import sys; sys.path.insert(0, {str(Path(__file__).parents[3] / 'examples')!r})\n"
            """
from document_review.contracts import definition
assert definition.key == 'document.review'
assert 'document_review.workflow' not in sys.modules
assert 'pydantic_ai_harness' not in sys.modules
""",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr

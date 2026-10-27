"""Task failure rules over real authorization/SQL/SDK, without restarting a Worker."""

from uuid import UUID, uuid4

import httpx2
import pytest
from sqlalchemy import select
from support import model_stub
from test_files_dictionary import client as client
from test_identity import query
from test_image_tasks import accept
from test_model_access import model_configuration as model_configuration

from keystone.core.errors import ServiceError
from keystone.core.worker_identity import WorkerIdentity
from keystone.platform.files.models import File
from keystone.platform.flow.models import FlowRun
from keystone.platform.flow.services.identity import ExecutionIdentity
from keystone.platform.model_access.models import ModelInvocation
from keystone.platform.model_access.schemas.errors import AdapterError
from keystone.platform.model_access.schemas.images import ImageTaskRequest
from keystone.platform.model_access.schemas.invocations import InvocationContext, Selection
from keystone.platform.model_access.services.entries import ModelEntryService
from keystone.platform.model_access.services.image_tasks import ImageTaskService

pytestmark = pytest.mark.postgres


def submit(client, monkeypatch, *, condition=None):
    run_id = accept(client, monkeypatch, engine=False)
    runtime = client.app.state.runtime
    run = query(client, lambda session: session.get(FlowRun, UUID(run_id)))
    selected = Selection.model_validate(
        run.definition_snapshot["model_bindings"]["generate"]["selection"]
    )
    context = InvocationContext(flow_run_id=run.id, node_key="generate", model_role="generate")
    tasks = ImageTaskService(ModelEntryService(ExecutionIdentity(runtime, run.id), runtime.models))
    requests = []

    async def respond(request):
        requests.append((request.method, request.url.path, request.url.host))
        if "/kling/v1/images/generations/" in request.url.path and condition == "source":
            return httpx2.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "task_id": request.url.path.rsplit("/", 1)[1],
                        "task_status": "succeed",
                        "task_result": {"images": [{"url": "https://127.0.0.1/image.png"}]},
                    },
                },
            )
        if request.url.host == "p2-fdl.klingai.com" and condition == "invalid_image":
            return httpx2.Response(200, content=b"not an image")
        return await model_stub.transport("openlux").handle_async_request(request)

    monkeypatch.setattr(runtime.models.http, "transport", lambda _: httpx2.MockTransport(respond))
    owner = WorkerIdentity(runtime.settings.worker_id, uuid4(), runtime.execution_version())

    async def send():
        return await tasks.submit(
            ImageTaskRequest.model_validate(run.input),
            selected=selected,
            identifier=uuid4(),
            context=context,
            worker_identity=owner,
        )

    handle = client.portal.call(send)
    original = query(client, lambda session: session.get(ModelInvocation, handle.invocation_id))
    assert original.status == "running" and original.result_context["kind"] == "task"
    return tasks, selected, context, original, requests


@pytest.mark.parametrize(
    "condition,status,error",
    [
        ("source", "unknown", "model_result_source_denied"),
        ("invalid_image", "succeeded", "model_invalid_image"),
    ],
)
def test_invalid_task_outputs_preserve_the_persisted_call_facts(
    client, monkeypatch, condition, status, error
):
    tasks, selected, context, original, requests = submit(client, monkeypatch, condition=condition)

    async def poll():
        with pytest.raises((ServiceError, AdapterError)) as caught:
            await tasks.poll(original.id, selected=selected, context=context)
        assert caught.value.code == error

    client.portal.call(poll)
    rows = query(client, lambda session: session.scalars(select(ModelInvocation))).all()
    assert len(rows) == 1 and rows[0].id == original.id and rows[0].status == status
    assert rows[0].flow_context == original.flow_context
    assert rows[0].result_context["task_id"] == original.result_context["task_id"]
    assert rows[0].upstream_request_id == original.upstream_request_id
    assert rows[0].cost_amount is None and rows[0].cost_check_status == "pending"
    assert query(client, lambda session: session.scalar(select(File.id))) is None
    assert sum(method == "POST" for method, _, _ in requests) == 1
    assert sum("/kling/v1/images/generations/" in path for _, path, _ in requests) == 1
    assert any(host == "p2-fdl.klingai.com" for _, _, host in requests) == (
        condition == "invalid_image"
    )


def test_exhausted_task_queries_commit_unknown_without_another_http_request(client, monkeypatch):
    tasks, selected, context, original, requests = submit(client, monkeypatch)

    async def exhaust(session):
        row = await session.get(ModelInvocation, original.id)
        row.result_context = {**row.result_context, "queries": 60}

    query(client, exhaust)

    async def poll():
        with pytest.raises(ServiceError) as caught:
            await tasks.poll(original.id, selected=selected, context=context)
        assert caught.value.code == "model_task_timeout"

    client.portal.call(poll)
    rows = query(client, lambda session: session.scalars(select(ModelInvocation))).all()
    assert len(rows) == 1 and rows[0].id == original.id and rows[0].status == "unknown"
    assert rows[0].error_code == "model_task_timeout" and rows[0].finished_at is not None
    assert rows[0].result_context["task_id"] == original.result_context["task_id"]
    assert rows[0].result_context["queries"] == 60
    assert rows[0].flow_context == original.flow_context
    assert rows[0].cost_amount is None and rows[0].cost_check_status == "pending"
    assert requests == [("POST", "/kling/v1/images/generations", "api.openlux.ai")]
    assert query(client, lambda session: session.scalar(select(File.id))) is None

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


def submit(client, monkeypatch, *, condition=None, price_config=None):
    run_id = accept(client, monkeypatch, engine=False)
    runtime = client.app.state.runtime
    run = query(client, lambda session: session.get(FlowRun, UUID(run_id)))
    selected = Selection.model_validate(
        run.definition_snapshot["model_bindings"]["generate"]["selection"]
    )
    if price_config is not None:
        response = client.put(
            f"/api/v1/model-entries/{selected.model_entry_id}/pricing",
            json={"expected_revision": 0, "config": price_config},
        )
        assert response.status_code == 200, response.text
    context = InvocationContext(flow_run_id=run.id, node_key="generate", model_role="generate")
    tasks = ImageTaskService(ModelEntryService(ExecutionIdentity(runtime, run.id), runtime.models))
    requests = []

    failed_once = False

    async def respond(request):
        nonlocal failed_once
        requests.append((request.method, request.url.path, request.url.host))
        if not failed_once and (
            condition == "query429"
            and "/kling/v1/images/generations/" in request.url.path
            or condition == "download_error"
            and request.url.host == "p2-fdl.klingai.com"
        ):
            failed_once = True
            return httpx2.Response(
                429 if condition == "query429" else 500, headers={"retry-after": "1"}
            )
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


@pytest.mark.parametrize("condition", ["download_error", "query429"])
def test_original_task_resumes_after_a_persisted_delay(client, monkeypatch, condition):
    import asyncio

    from keystone.platform.model_access.models import ModelDispatchScope
    from keystone.platform.model_access.schemas.images import ImageBatch, ImagePending

    tasks, selected, context, original, requests = submit(client, monkeypatch, condition=condition)
    pending = client.portal.call(
        lambda: tasks.poll(original.id, selected=selected, context=context)
    )
    assert isinstance(pending, ImagePending)
    before = query(client, lambda s: s.get(ModelInvocation, original.id))
    assert before.result_context["queries"] == 1
    assert before.result_context["deadline_at"] == original.result_context["deadline_at"]
    scope = query(client, lambda s: s.scalar(select(ModelDispatchScope)))
    if condition == "query429":
        assert scope.blocked_until > scope.updated_at
    resumed = ImageTaskService(ModelEntryService(tasks.identity, tasks.clients))
    count = len(requests)
    assert isinstance(
        client.portal.call(lambda: resumed.poll(original.id, selected=selected, context=context)),
        ImagePending,
    )
    assert len(requests) == count
    if condition == "query429":
        client.portal.call(asyncio.sleep, 1.1)

    async def due(session):
        row = await session.get(ModelInvocation, original.id)
        row.result_context = {
            **row.result_context,
            "next_query_at": row.result_context["submitted_at"],
        }

    query(client, due)
    result = client.portal.call(
        lambda: resumed.poll(original.id, selected=selected, context=context)
    )
    assert isinstance(result, ImageBatch)
    row = query(client, lambda s: s.get(ModelInvocation, original.id))
    assert row.status == "succeeded" and row.result_context["kind"] == "images"
    assert row.result_context["task"]["task_id"] == original.result_context["task_id"]
    assert row.result_context["task"]["deadline_at"] == original.result_context["deadline_at"]
    assert row.flow_context == original.flow_context
    assert sum(method == "POST" for method, _, _ in requests) == 1
    assert query(client, lambda s: s.scalar(select(File.id))) is None
    assert len(query(client, lambda s: s.scalars(select(ModelInvocation))).all()) == 1
    if condition == "query429":
        assert (
            query(client, lambda s: s.scalar(select(ModelDispatchScope))).blocked_until
            == scope.blocked_until
        )


def test_disabled_task_stops_before_query_and_preserves_cleanup(client, monkeypatch):
    tasks, selected, context, original, requests = submit(client, monkeypatch)
    response = client.patch(
        f"/api/v1/model-entries/{selected.model_entry_id}",
        json={
            "channel_id": str(selected.channel_id),
            "provider_model": selected.provider_model,
            "is_active": False,
        },
    )
    assert response.status_code == 200, response.text

    async def query_and_close():
        with pytest.raises(ServiceError) as error:
            await tasks.poll(original.id, selected=selected, context=context)
        assert error.value.code == "model_configuration_changed"
        await tasks.abandon(original.id, selected, context)

    client.portal.call(query_and_close)
    row = query(client, lambda s: s.get(ModelInvocation, original.id))
    assert (
        row.status == "unknown"
        and row.result_context["task_id"] == original.result_context["task_id"]
    )
    assert (
        row.flow_context == original.flow_context
        and row.upstream_request_id == original.upstream_request_id
    )
    assert row.cost_amount == original.cost_amount
    assert requests == [("POST", "/kling/v1/images/generations", "api.openlux.ai")]
    assert query(client, lambda s: s.scalar(select(File.id))) is None

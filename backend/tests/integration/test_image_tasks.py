"""A durable native task is queried and downloaded without resubmitting generation."""

import json

import pytest
from sqlalchemy import select
from support.flow_packages import assert_correlated, install_package
from support.operation_flow import PACKAGE, task_definition
from test_files_dictionary import client as client
from test_flow import wait_run, worker
from test_identity import query
from test_model_access import channel, entry
from test_model_access import model_configuration as model_configuration

from keystone.platform.files.models import File, FileReference
from keystone.platform.flow.models import FlowRunBudget
from keystone.platform.model_access.models import ModelDispatchScope, ModelInvocation

pytestmark = pytest.mark.postgres


def accept(client, monkeypatch):
    install_package(client, monkeypatch, PACKAGE)
    entry(
        client,
        channel(client, provider="openlux", api_base="https://api.openlux.ai/v1"),
        provider_model="kling-v1-5",
        operations=["image.generate"],
    )
    response = client.post(
        "/api/v1/flow-runs",
        json={
            "definition_key": task_definition.key,
            "request_key": "task",
            "input": {"prompt": "A square"},
            "model_bindings": {"generate": "chat"},
        },
    )
    assert response.status_code == 202, response.text
    return response.json()["id"]


def code(request_log, task_state, crash=None):
    return f"""import json, os
from pathlib import Path
import httpx2
from support import model_stub
model_stub.IMAGE_TASK_STATE = {str(task_state)!r}
from keystone import composition
from support.operation_flow import PACKAGE
composition.FLOW_PACKAGES = (PACKAGE,)
composition.execution_version.cache_clear()
from keystone.platform.model_access.adapters.http import HttpClient
original_transport = HttpClient.transport
def transport(self, provider):
    underlying = original_transport(self, provider)
    async def send(request):
        with Path({str(request_log)!r}).open('a') as log:
            log.write(json.dumps({{'method': request.method, 'path': request.url.path,
                'host': request.headers.get('host'), 'auth': 'authorization' in request.headers,
                'sni': request.extensions.get('sni_hostname')}}) + '\\n')
        if request.method == 'POST' and {crash!r} == 'registered':
            os._exit(71)
        if request.method == 'GET' and '/kling/v1/images/generations/' in request.url.path:
            if {crash!r} == 'query429':
                return httpx2.Response(429, headers={{'retry-after': '2'}})
            if {crash!r} == 'failed':
                return httpx2.Response(200, json={{'code': 0, 'data': {{'task_status': 'failed'}}}})
            if {crash!r} == 'source':
                return httpx2.Response(200, json={{'code': 0, 'data': {{'task_status': 'succeed',
                    'task_result': {{'images': [{{'url': 'https://127.0.0.1/image.png'}}]}}}}}})
        if request.headers.get('host') == 'p2-fdl.klingai.com':
            if {crash!r} == 'invalid_image':
                return httpx2.Response(200, content=b'not an image')
            marker = Path({str(request_log)!r}).with_suffix('.download-error')
            if {crash!r} == 'download_error' and not marker.exists():
                marker.write_text('failed once')
                return httpx2.Response(500)
        return await underlying.handle_async_request(request)
    return httpx2.MockTransport(send)
HttpClient.transport = transport
if {crash!r} == 'receipt':
    from keystone.platform.model_access.services.image_tasks import ImageTaskService
    original_submit = ImageTaskService.submit
    async def submit(self, *args, **kwargs):
        await original_submit(self, *args, **kwargs)
        os._exit(71)
    ImageTaskService.submit = submit
if {crash!r} == 'result':
    from keystone.platform.model_access.services.image_tasks import ImageTaskService
    from keystone.platform.model_access.schemas.images import ImageBatch
    original_poll = ImageTaskService.poll
    async def poll(self, *args, **kwargs):
        result = await original_poll(self, *args, **kwargs)
        if isinstance(result, ImageBatch):
            os._exit(71)
        return result
    ImageTaskService.poll = poll
if {crash!r} in {{'download_error', 'query429'}}:
    from keystone.platform.model_access.services.image_tasks import ImageTaskService
    original_delay = ImageTaskService.delay
    async def delay(self, *args, **kwargs):
        result = await original_delay(self, *args, **kwargs)
        failed_download = Path({str(request_log)!r}).with_suffix('.download-error').exists()
        if {crash!r} == 'query429' or failed_download:
            os._exit(71)
        return result
    ImageTaskService.delay = delay
if {crash!r} == 'file':
    from keystone.platform.flow.workflows.context import FlowContext
    original_upload = FlowContext.upload
    async def upload(self, *args, **kwargs):
        await original_upload(self, *args, **kwargs)
        os._exit(71)
    FlowContext.upload = upload
from keystone.entrypoints.worker import main
main()
"""


@pytest.mark.parametrize("crash", [None, "receipt", "result", "file", "download_error", "query429"])
def test_native_task_recovery_preserves_original_submission(client, monkeypatch, tmp_path, crash):
    run_id = accept(client, monkeypatch)
    request_log, task_state = tmp_path / "requests.jsonl", tmp_path / "vendor.json"
    before_files = []
    if crash:
        with worker(client, tmp_path, code=code(request_log, task_state, crash)) as (process, log):
            assert process.wait(timeout=35) == 71, log.read_text()
        before = query(client, lambda s: s.scalar(select(ModelInvocation)))
        before_files = query(client, lambda s: s.scalars(select(File))).all()
        assert len(before_files) == (1 if crash == "file" else 0)
        if crash == "query429":
            cooldown = query(client, lambda s: s.scalar(select(ModelDispatchScope)))
            assert cooldown.blocked_until > cooldown.updated_at
    with worker(client, tmp_path, code=code(request_log, task_state)) as (process, log):
        done = wait_run(client, run_id, process, log)
        assert done["status"] == "succeeded", (done, log.read_text())
    calls = query(client, lambda s: s.scalars(select(ModelInvocation))).all()
    assert len(calls) == 1 and calls[0].status == "succeeded"
    assert_correlated(client, run_id, calls)
    budget = query(client, lambda s: s.get(FlowRunBudget, calls[0].flow_context["flow_run_id"]))
    assert budget.counters["model_requests"] == 1
    assert budget.counters["output_tokens"] == budget.counters["unreported_text_requests"] == 0
    if crash == "query429":
        current_scope = query(client, lambda s: s.scalar(select(ModelDispatchScope)))
        assert current_scope.blocked_until == cooldown.blocked_until
    if crash:
        assert calls[0].id == before.id
        assert calls[0].flow_context == before.flow_context
        original = before.result_context.get("task") or before.result_context
        current = calls[0].result_context["task"]
        assert current["deadline_at"] == original["deadline_at"]
        assert current["queries"] >= original["queries"]
        assert calls[0].upstream_request_id == before.upstream_request_id
    manifest = calls[0].result_context
    assert manifest["kind"] == "images" and manifest["task"]["queries"] >= 2
    assert "https://" not in str(manifest) and "prompt" not in str(manifest)
    sent = [json.loads(line) for line in request_log.read_text().splitlines()]
    submissions = [r for r in sent if r["method"] == "POST"]
    assert len(submissions) == 1 and submissions[0]["path"] == "/kling/v1/images/generations"
    queries = [r for r in sent if r["path"].startswith("/kling/v1/images/generations/")]
    assert queries and {r["path"].rsplit("/", 1)[1] for r in queries} == {
        manifest["task"]["task_id"]
    }
    downloads = [r for r in sent if r["host"] == "p2-fdl.klingai.com"]
    assert downloads and all(not r["auth"] and r["sni"] == "p2-fdl.klingai.com" for r in downloads)
    files = query(client, lambda s: s.scalars(select(File))).all()
    assert len(files) == 1
    assert done["result"]["files"] == [{"file_id": str(files[0].id)}]
    if crash == "file":
        assert files[0].id == before_files[0].id
    refs = query(
        client,
        lambda s: s.scalars(select(FileReference).where(FileReference.owner == f"flow:{run_id}")),
    ).all()
    assert {r.slot for r in refs} == {"output", f"artifact.model.{calls[0].id}.0"}
    assert (
        client.get(f"/api/v1/model-invocations/{calls[0].id}").json()["operation"]
        == "image.generate"
    )


@pytest.mark.parametrize("condition", ["deadline", "queries", "cancel", "disabled"])
def test_task_limits_and_cancellation_survive_restart_without_new_requests(
    client, monkeypatch, tmp_path, condition
):
    from datetime import timedelta

    from keystone.core.time import utcnow

    run_id = accept(client, monkeypatch)
    request_log, task_state = tmp_path / "requests.jsonl", tmp_path / "vendor.json"
    with worker(client, tmp_path, code=code(request_log, task_state, "receipt")) as (process, log):
        assert process.wait(timeout=35) == 71, log.read_text()
    original = query(client, lambda s: s.scalar(select(ModelInvocation)))
    if condition in {"deadline", "queries"}:

        async def expired(session):
            row = await session.get(ModelInvocation, original.id)
            evidence = dict(row.result_context)
            if condition == "deadline":
                deadline = utcnow() - timedelta(seconds=1)
                evidence.update(
                    submitted_at=(deadline - timedelta(seconds=300)).isoformat(),
                    deadline_at=deadline.isoformat(),
                    next_query_at=deadline.isoformat(),
                )
            else:
                evidence["queries"] = 60
            row.result_context = evidence

        query(client, expired)
    elif condition == "cancel":
        response = client.post(f"/api/v1/flow-runs/{run_id}/cancel")
        assert response.status_code == 200, response.text
    else:
        response = client.patch(
            f"/api/v1/model-entries/{original.model_entry_id}",
            json={
                "channel_id": str(original.channel_id),
                "provider_model": original.provider_model,
                "is_active": False,
            },
        )
        assert response.status_code == 200
    with worker(client, tmp_path, code=code(request_log, task_state)) as (process, log):
        done = wait_run(client, run_id, process, log)
        assert done["status"] == ("cancelled" if condition == "cancel" else "failed"), (
            done,
            log.read_text(),
        )
    sent = [json.loads(line) for line in request_log.read_text().splitlines()]
    assert len([r for r in sent if r["method"] == "POST"]) == 1
    assert not [r for r in sent if "/kling/v1/images/generations/" in r["path"]]
    call = query(client, lambda s: s.get(ModelInvocation, original.id))
    assert (
        call.status == "unknown"
        and call.result_context["task_id"] == original.result_context["task_id"]
    )
    assert query(client, lambda s: s.scalar(select(File.id))) is None
    if condition != "cancel":
        expected = (
            "model_configuration_changed" if condition == "disabled" else "model_task_timeout"
        )
        assert done["error_code"] == expected


@pytest.mark.parametrize(
    "condition,model_status,error",
    [
        ("registered", "unknown", "model_result_unavailable"),
        ("failed", "failed", "model_task_failed"),
        ("source", "unknown", "model_result_source_denied"),
        ("invalid_image", "succeeded", "model_invalid_image"),
    ],
)
def test_task_failure_and_invalid_artifacts_preserve_separate_facts(
    client, monkeypatch, tmp_path, condition, model_status, error
):
    run_id = accept(client, monkeypatch)
    request_log, task_state = tmp_path / "requests.jsonl", tmp_path / "vendor.json"
    if condition == "registered":
        with worker(client, tmp_path, code=code(request_log, task_state, condition)) as (
            process,
            log,
        ):
            assert process.wait(timeout=35) == 71, log.read_text()
    active = None if condition == "registered" else condition
    with worker(client, tmp_path, code=code(request_log, task_state, active)) as (process, log):
        done = wait_run(client, run_id, process, log)
        assert done["status"] == "failed" and done["error_code"] == error, (done, log.read_text())
    calls = query(client, lambda s: s.scalars(select(ModelInvocation))).all()
    assert len(calls) == 1 and calls[0].status == model_status
    assert query(client, lambda s: s.scalar(select(File.id))) is None
    sent = [json.loads(line) for line in request_log.read_text().splitlines()]
    assert len([r for r in sent if r["method"] == "POST"]) == 1
    if condition == "source":
        assert not [r for r in sent if r["host"] == "p2-fdl.klingai.com"]

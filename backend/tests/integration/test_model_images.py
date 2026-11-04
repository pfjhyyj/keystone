"""Native Worker generation/edit and durable Files recovery, without paid requests."""

import json
from io import BytesIO

import pytest
from PIL import Image
from sqlalchemy import select
from support.flow_packages import assert_correlated, install_package
from support.operation_flow import PACKAGE, edit_definition, image_definition
from test_files_dictionary import client as client
from test_files_dictionary import stored_content, upload
from test_flow import wait_run, worker
from test_identity import query
from test_model_access import channel, entry
from test_model_access import model_configuration as model_configuration

from keystone.platform.files.models import File, FileReference
from keystone.platform.model_access.models import ModelInvocation

pytestmark = pytest.mark.postgres


def accept(client, monkeypatch, *, edit=False, n=1, headers=None):
    install_package(client, monkeypatch, PACKAGE)
    role, definition = ("edit", edit_definition) if edit else ("generate", image_definition)
    entry(client, channel(client), operations=["image.edit" if edit else "image.generate"])
    inputs = {"prompt": "A blue square", "n": n}
    if edit:
        content = BytesIO()
        Image.new("RGB", (2, 2), "red").save(content, format="PNG")
        uploaded = upload(
            client, content.getvalue(), content_type="image/png", name="reference.png"
        )
        assert uploaded.status_code == 201, uploaded.text
        inputs["images"] = [{"file_id": uploaded.json()["id"]}]
    response = client.post(
        "/api/v1/_test/review",
        json={
            "definition_key": definition.key,
            "request_key": "images",
            "input": inputs,
            "model_bindings": {role: "chat"},
        },
        headers=headers,
    )
    assert response.status_code == 202, response.text
    return response.json()["id"]


def code(request_log, crash=None):
    return f"""import json, os
from dbos import DBOS
from pathlib import Path
import httpx2
from keystone import composition
from support.operation_flow import PACKAGE
composition.FLOW_PACKAGES = (PACKAGE,)
composition.execution_version.cache_clear()
from keystone.platform.model_access.adapters.http import HttpClient
original_transport = HttpClient.transport
def transport(self, provider):
    underlying = original_transport(self, provider)
    async def send(request):
        if request.method == 'POST':
            body = json.loads(request.content)
            with Path({str(request_log)!r}).open('a') as log:
                log.write(json.dumps({{'id': request.headers['x-keystone-invocation-id'],
                    'path': request.url.path, 'model': body['model'],
                    'references': len(body.get('input_references', [])),
                    'workflow_id': DBOS.workflow_id, 'step_id': DBOS.step_id}}) + '\\n')
            if {crash!r} == 'registered':
                os._exit(71)
        return await underlying.handle_async_request(request)
    return httpx2.MockTransport(send)
HttpClient.transport = transport
if {crash!r} == 'response':
    from keystone.platform.model_access.adapters.inference import images
    original_batch = images.image_batch
    def batch(*args):
        original_batch(*args)
        os._exit(71)
    images.image_batch = batch
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


def invocations(client):
    return query(client, lambda s: s.scalars(select(ModelInvocation))).all()


def references(client, run_id):
    return query(
        client,
        lambda s: s.scalars(select(FileReference).where(FileReference.owner == f"flow:{run_id}")),
    ).all()


@pytest.mark.parametrize("edit", [False, True])
def test_image_generation_and_edit_produce_controlled_files(client, monkeypatch, tmp_path, edit):
    run_id = accept(client, monkeypatch, edit=edit, n=2)
    request_log = tmp_path / "requests.jsonl"
    with worker(client, tmp_path, code=code(request_log)) as (process, log):
        done = wait_run(client, run_id, process, log)
        assert done["status"] == "succeeded", (done, log.read_text())
    calls = invocations(client)
    assert len(calls) == 1 and calls[0].status == "succeeded"
    assert_correlated(client, run_id, calls)
    assert calls[0].operation == ("image.edit" if edit else "image.generate")
    assert calls[0].result_context["kind"] == "images"
    assert "b64_json" not in str(calls[0].result_context)
    assert "prompt" not in str(calls[0].result_context)
    sent = [json.loads(line) for line in request_log.read_text().splitlines()]
    assert sent == [
        {
            "id": str(calls[0].id),
            "path": "/api/v1/images",
            "model": "mock-chat",
            "references": 1 if edit else 0,
            "workflow_id": calls[0].flow_context["workflow_id"],
            "step_id": calls[0].flow_context["step_id"],
        }
    ]
    outputs = done["result"]["files"]
    assert len(outputs) == 2 and len({file["file_id"] for file in outputs}) == 2
    refs = references(client, run_id)
    assert {str(r.file_id) for r in refs if r.slot == "output"} == {
        file["file_id"] for file in outputs
    }
    for item in outputs:
        content = stored_content(client, item["file_id"])
        Image.open(BytesIO(content)).verify()
    public = client.get(f"/api/v1/model-invocations/{calls[0].id}").json()
    assert public["operation"] == calls[0].operation and "result_context" not in public


@pytest.mark.parametrize("crash", ["registered", "response", "file"])
def test_image_recovery_keeps_original_call_and_file(client, monkeypatch, tmp_path, crash):
    run_id = accept(client, monkeypatch)
    request_log = tmp_path / "requests.jsonl"
    with worker(client, tmp_path, code=code(request_log, crash)) as (process, log):
        assert process.wait(timeout=35) == 71, log.read_text()
    before = invocations(client)
    assert len(before) == 1
    files = query(client, lambda s: s.scalars(select(File))).all()
    assert len(files) == (1 if crash == "file" else 0)
    with worker(client, tmp_path, code=code(request_log)) as (process, log):
        done = wait_run(client, run_id, process, log)
        assert done["status"] == ("succeeded" if crash == "file" else "failed"), (
            done,
            log.read_text(),
        )
    after = invocations(client)
    assert len(after) == 1 and after[0].id == before[0].id
    assert after[0].flow_context == before[0].flow_context
    assert_correlated(client, run_id, after)
    assert len(request_log.read_text().splitlines()) == 1
    if crash == "file":
        assert done["result"]["files"] == [{"file_id": str(files[0].id)}]
        assert after[0].usage == before[0].usage and after[0].cost_amount == before[0].cost_amount
        assert {r.slot for r in references(client, run_id)} == {
            f"artifact.model.{before[0].id}.0",
            "output",
        }
        assert len(query(client, lambda s: s.scalars(select(File))).all()) == 1
    else:
        assert done["error_code"] == "model_result_unavailable"
        assert after[0].status == "unknown"
        assert references(client, run_id) == []


@pytest.mark.parametrize("revoke", [False, True])
def test_inflight_image_receipt_survives_cancel_or_pat_revocation_without_publishing_files(
    client, monkeypatch, tmp_path, revoke
):
    import time

    from test_identity import bearer, token

    pat = token(
        client,
        ["flow:start", "model:invoke", "test_files.asset:create", "test_files.asset:content_read"],
    )
    run_id = accept(client, monkeypatch, headers=bearer(pat["token"]))
    request_log = tmp_path / "inflight.jsonl"
    script = "import asyncio\n" + code(request_log).replace(
        "        return await underlying.handle_async_request(request)",
        "        if request.method == 'POST':\n"
        "            await asyncio.sleep(4)\n"
        "        return await underlying.handle_async_request(request)",
    )
    with worker(client, tmp_path, code=script) as (process, log):
        deadline = time.monotonic() + 15
        while not request_log.exists():
            assert process.poll() is None and time.monotonic() < deadline, log.read_text()
            time.sleep(0.05)
        if revoke:
            assert client.delete(f"/api/v1/auth/tokens/{pat['id']}").status_code == 204
        else:
            assert client.post(f"/api/v1/flow-runs/{run_id}/cancel").status_code == 200
        done = wait_run(client, run_id, process, log)
        assert done["status"] == ("failed" if revoke else "cancelled"), (done, log.read_text())
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            calls = invocations(client)
            if calls and calls[0].status != "running":
                break
            time.sleep(0.05)
        assert len(calls) == 1 and calls[0].status == "succeeded", (done, log.read_text())
        assert_correlated(client, run_id, calls, require_step=False)
        sent = json.loads(request_log.read_text().splitlines()[0])
        assert sent["workflow_id"] == calls[0].flow_context["workflow_id"]
        assert sent["step_id"] == calls[0].flow_context["step_id"]
        assert calls[0].result_context["kind"] == "images" and calls[0].cost_source == "response"
    assert query(client, lambda s: s.scalar(select(File.id))) is None
    assert len(request_log.read_text().splitlines()) == 1

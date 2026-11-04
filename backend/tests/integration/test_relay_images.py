"""Real PostgreSQL/DBOS image delivery and exact original-token cost reconciliation."""

import json
from io import BytesIO

import pytest
from PIL import Image
from sqlalchemy import select
from support.flow_packages import install_package
from support.operation_flow import PACKAGE, edit_definition, image_definition
from support.relay_images import install
from test_auto_costs import due, process, stored, view
from test_files_dictionary import client as client
from test_files_dictionary import upload
from test_flow import wait_run, worker
from test_identity import query
from test_model_access import channel, entry
from test_model_access import model_configuration as model_configuration

from keystone.platform.files.models import File
from keystone.platform.model_access.models import ModelInvocation

pytestmark = pytest.mark.postgres


def accept(client, monkeypatch, provider, *, edit=False, headers=None):
    install_package(client, monkeypatch, PACKAGE)
    role, definition = ("edit", edit_definition) if edit else ("generate", image_definition)
    base = "https://api.openlux.ai/v1" if provider == "openlux" else "https://yunwu.ai/v1"
    config = channel(client, provider, api_base=base)
    entry(
        client,
        config,
        provider_model="gpt-image-2",
        operations=["image.edit" if edit else "image.generate"],
    )
    inputs = {"prompt": "A blue square", "n": 2, "quality": "medium", "aspect_ratio": "1:1"}
    if edit:
        content = BytesIO()
        Image.new("RGB", (2, 2), "red").save(content, format="PNG")
        files = [
            upload(
                client,
                content.getvalue(),
                key=f"ref-{i}",
                content_type="image/png",
                name=f"ref-{i}.png",
            ).json()["id"]
            for i in range(9)
        ]
        inputs["images"] = [{"file_id": file} for file in files]
    response = client.post(
        "/api/v1/_test/review",
        json={
            "definition_key": definition.key,
            "request_key": "relay",
            "input": inputs,
            "model_bindings": {role: "chat"},
        },
        headers=headers,
    )
    assert response.status_code == 202, response.text
    return response.json()["id"]


def code(request_log, *, crash=None, pause=False, **options):
    return f"""import os
from pathlib import Path
from keystone import composition
from support.operation_flow import PACKAGE
composition.FLOW_PACKAGES = (PACKAGE,)
composition.execution_version.cache_clear()
from support.relay_images import install
install({str(request_log)!r}, crash={crash!r}, **{options!r})
if {pause!r}:
    import asyncio
    import httpx2
    from keystone.platform.model_access.adapters.http import HttpClient
    original_transport = HttpClient.transport
    def transport(self, provider):
        underlying = original_transport(self, provider)
        async def respond(request):
            if request.method == 'GET' and request.headers.get('host') == 'cdn.example.test':
                Path({str(request_log.with_suffix(".waiting"))!r}).touch()
                while not Path({str(request_log.with_suffix(".release"))!r}).exists():
                    await asyncio.sleep(0.05)
            return await underlying.handle_async_request(request)
        return httpx2.MockTransport(respond)
    HttpClient.transport = transport
if {crash!r} in {{'sources', 'manifest'}}:
    from keystone.platform.model_access.services.image_results import ImageResultService
    original = ImageResultService.fetch
    async def fetch(self, *args, **kwargs):
        if {crash!r} == 'sources':
            os._exit(71)
        result = await original(self, *args, **kwargs)
        os._exit(71)
    ImageResultService.fetch = fetch
if {crash!r} == 'file':
    from keystone.platform.flow.workflows.context import FlowContext
    original_upload = FlowContext.upload
    async def upload(self, *args, **kwargs):
        result = await original_upload(self, *args, **kwargs)
        os._exit(71)
    FlowContext.upload = upload
from keystone.entrypoints.worker import main
main()
"""


@pytest.mark.parametrize(
    "provider,edit,kind", [("yunwu", False, "url"), ("openlux", True, "base64")]
)
def test_relay_images_and_actual_cost_are_independent_and_idempotent(
    client, monkeypatch, tmp_path, provider, edit, kind
):
    request_log = tmp_path / "requests.jsonl"
    run_id = accept(client, monkeypatch, provider, edit=edit)
    with worker(client, tmp_path, code=code(request_log, kind=kind, retry=True)) as (
        process_handle,
        log,
    ):
        done = wait_run(client, run_id, process_handle, log)
        assert done["status"] == "succeeded", (done, log.read_text())
    calls = query(client, lambda s: s.scalars(select(ModelInvocation))).all()
    assert len(calls) == 1 and calls[0].status == "succeeded"
    identifier = str(calls[0].id)
    assert calls[0].upstream_request_id == "bill-" + identifier
    assert len(done["result"]["files"]) == 2
    assert calls[0].cost_amount is None or str(calls[0].cost_amount) == "0.002656000000000000"
    # Run the same billing facade in the API process against the same deterministic receipts.
    with monkeypatch.context() as patch:
        from keystone.platform.model_access.adapters.http import HttpClient

        patch.setattr(HttpClient, "transport", HttpClient.transport)
        install(request_log)
        due(client, identifier)
        process(client, identifier)
        for _ in range(2):
            assert client.post(f"/api/v1/model-invocations/{identifier}/cost").status_code == 200
    current = view(client, identifier)
    assert current["cost_amount"] == "0.002656000000000000"
    assert current["cost_unit"] == ("USD" if provider == "openlux" else "yunwu_credits")
    assert current["cost_check_status"] == "confirmed" and current["cost_source"] == "query"
    assert "result_context" not in current
    evidence = json.dumps(stored(client, identifier).result_context)
    assert (
        "private-result-secret" not in evidence
        and "b64_json" not in evidence
        and "A blue square" not in evidence
    )
    requests = [json.loads(line) for line in request_log.read_text().splitlines()]
    posts = [r for r in requests if r["method"] == "POST"]
    assert len(posts) == 1 and posts[0]["references"] == (9 if edit else 0)
    assert all(
        not r["auth"] and r["host"] == "cdn.example.test" for r in requests if r["method"] == "GET"
    )


@pytest.mark.parametrize("crash", ["registered", "sources", "manifest", "file"])
def test_relay_url_recovery_never_repeats_generation(client, monkeypatch, tmp_path, crash):
    run_id = accept(client, monkeypatch, "yunwu")
    request_log = tmp_path / "requests.jsonl"
    with worker(client, tmp_path, code=code(request_log, crash=crash)) as (process_handle, log):
        assert process_handle.wait(timeout=35) == 71, log.read_text()
    before = query(client, lambda s: s.scalars(select(ModelInvocation))).one()
    if crash != "registered":
        assert before.status == "succeeded" and "private-result-secret" not in json.dumps(
            before.result_context
        )
    with worker(client, tmp_path, code=code(request_log)) as (process_handle, log):
        done = wait_run(client, run_id, process_handle, log)
        assert done["status"] == ("failed" if crash == "registered" else "succeeded"), (
            done,
            log.read_text(),
        )
    after = query(client, lambda s: s.scalars(select(ModelInvocation))).one()
    assert after.id == before.id and after.flow_context == before.flow_context
    requests = [json.loads(line) for line in request_log.read_text().splitlines()]
    assert sum(r["method"] == "POST" for r in requests) == 1
    if crash != "registered":
        assert len(query(client, lambda s: s.scalars(select(File))).all()) == 2
        assert after.result_context["kind"] == "images"
    else:
        assert after.status == "unknown" and done["error_code"] == "model_result_unavailable"


def test_image_failures_keep_real_cost_and_delivery_facts_separate(client, monkeypatch, tmp_path):
    request_log = tmp_path / "requests.jsonl"
    run_id = accept(client, monkeypatch, "openlux")
    with worker(client, tmp_path, code=code(request_log, local=True)) as (handle, log):
        done = wait_run(client, run_id, handle, log)
        assert done["status"] == "succeeded", (
            done,
            log.read_text(),
        )
    call = query(client, lambda s: s.scalars(select(ModelInvocation))).one()
    identifier = str(call.id)
    with monkeypatch.context() as patch:
        from keystone.platform.model_access.adapters.http import HttpClient

        patch.setattr(HttpClient, "transport", HttpClient.transport)
        install(request_log)
        due(client, identifier)
        process(client, identifier)
    current = stored(client, identifier)
    assert str(current.cost_amount) == "0.002656000000000000"
    assert current.cost_check_status == "confirmed"
    assert current.status == "succeeded"
    files = query(client, lambda s: s.scalars(select(File))).all()
    assert files
    requests = [json.loads(line) for line in request_log.read_text().splitlines()]
    assert sum(r["method"] == "POST" for r in requests) == 1


def test_image_model_billing_conflict_preserves_successful_delivery(client, monkeypatch, tmp_path):
    # These are billing rules, independent of DBOS checkpoints. Keep real image
    # inference, original receipts, SQL reconciliation and Files publication.
    from keystone.platform.model_access.adapters.http import HttpClient

    configured = channel(client, "openlux", api_base="https://api.openlux.ai/v1")
    entry(client, configured, provider_model="gpt-image-2", operations=["image.generate"])
    request_log = tmp_path / "requests.jsonl"
    monkeypatch.setattr(HttpClient, "transport", HttpClient.transport)
    install(request_log)
    response = client.post(
        "/api/v1/_test/generate-image", json={"model_key": "chat", "prompt": "A blue square"}
    )
    assert response.status_code == 200, response.text
    identifier = response.json()["invocation_id"]
    before = stored(client, identifier)
    install(
        request_log,
        model="different-model",
    )
    due(client, identifier)
    process(client, identifier)
    current = stored(client, identifier)
    assert current.cost_amount is None and current.cost_check_reason == "cost_receipt_conflict"
    assert current.cost_check_status == "blocked"
    assert current.status == before.status == "succeeded"
    assert current.finished_at == before.finished_at
    assert current.upstream_request_id == before.upstream_request_id
    assert current.result_context == before.result_context
    assert len(query(client, lambda s: s.scalars(select(ModelInvocation))).all()) == 1
    files = query(client, lambda s: s.scalars(select(File))).all()
    assert {str(file.id) for file in files} == {
        file["file_id"] for file in response.json()["files"]
    }
    requests = [json.loads(line) for line in request_log.read_text().splitlines()]
    assert sum(request["method"] == "POST" for request in requests) == 1


def test_cancel_during_result_download_preserves_call_without_publishing(
    client, monkeypatch, tmp_path
):
    import time

    run_id = accept(client, monkeypatch, "yunwu")
    request_log = tmp_path / "requests.jsonl"
    with worker(client, tmp_path, code=code(request_log, pause=True)) as (handle, log):
        deadline = time.monotonic() + 20
        while not request_log.with_suffix(".waiting").exists():
            assert handle.poll() is None and time.monotonic() < deadline, log.read_text()
            time.sleep(0.05)
        assert client.post(f"/api/v1/flow-runs/{run_id}/cancel").status_code == 200
        request_log.with_suffix(".release").touch()
        done = wait_run(client, run_id, handle, log)
        assert done["status"] == "cancelled", (done, log.read_text())
    call = query(client, lambda s: s.scalars(select(ModelInvocation))).one()
    assert call.status == "succeeded" and call.result_context["kind"] in {"download", "images"}
    assert not query(client, lambda s: s.scalars(select(File))).all()
    requests = [json.loads(line) for line in request_log.read_text().splitlines()]
    assert sum(r["method"] == "POST" for r in requests) == 1


@pytest.mark.parametrize("change", ["expired", "cipher", "disabled", "changed"])
def test_original_image_evidence_is_checked_without_a_new_generation(
    client, monkeypatch, tmp_path, change
):
    from datetime import timedelta
    from unittest.mock import AsyncMock
    from uuid import uuid4

    from test_identity import token
    from test_model_access import internal_service
    from test_model_media_api import configure

    from keystone.core.errors import ServiceError
    from keystone.core.time import utcnow
    from keystone.platform.model_access.adapters.http import HttpClient
    from keystone.platform.model_access.models import Channel
    from keystone.platform.model_access.schemas.images import ImageGenerateRequest, ImageManifest
    from keystone.platform.model_access.services.image_results import (
        ImageResultService,
        read_receipt,
    )
    from keystone.platform.model_access.services.runtime import ModelRuntime

    configure(client, provider="yunwu", operations=["image.generate"])
    entries = internal_service(client, token(client, ["model:invoke"])["token"])
    model = ModelRuntime(entries)
    selected = client.portal.call(lambda: entries.selection("media", operation="image.generate"))
    context = None
    monkeypatch.setattr(
        model,
        "recover_image",
        AsyncMock(side_effect=ServiceError(503, "test_result_boundary", "paused")),
    )
    monkeypatch.setattr(HttpClient, "transport", HttpClient.transport)
    log = tmp_path / "sources.jsonl"
    install(log)
    identifier = uuid4()

    async def generate():
        with pytest.raises(ServiceError) as paused:
            await model.invoke_image(
                ImageGenerateRequest(prompt="blue", n=2),
                images=(),
                selection=selected,
                invocation_id=identifier,
                context=context,
                worker_identity=None,
            )
        assert paused.value.code == "test_result_boundary"

    client.portal.call(generate)
    service = ImageResultService(model.identity, model.clients)
    row = query(client, lambda s: s.get(ModelInvocation, identifier))
    assert row.status == "succeeded" and row.result_context["kind"] == "download"
    if change == "changed":
        client.portal.call(
            lambda: service.fetch(
                identifier,
                selected=selected,
                context=context,
                evidence=read_receipt(row.result_context),
            )
        )
        install(log, download_format="JPEG")

    async def mutate(session):
        row = await session.get(ModelInvocation, identifier)
        if change in {"expired", "cipher"}:
            row.result_context = {
                **row.result_context,
                **(
                    {"deadline_at": (utcnow() - timedelta(seconds=1)).isoformat()}
                    if change == "expired"
                    else {"sources_encrypted": "invalid-ciphertext"}
                ),
            }
        if change == "disabled":
            (await session.get(Channel, selected.channel_id)).is_active = False

    query(client, mutate)
    original = query(client, lambda s: s.get(ModelInvocation, identifier)).result_context
    evidence = (
        ImageManifest.model_validate(original) if change == "changed" else read_receipt(original)
    )

    async def recover():
        with pytest.raises(ServiceError) as error:
            await service.fetch(identifier, selected=selected, context=context, evidence=evidence)
        assert error.value.code == (
            "model_credential_unavailable" if change == "cipher" else "model_result_unavailable"
        )

    client.portal.call(recover)
    sent = [json.loads(line) for line in log.read_text().splitlines()]
    assert sum(r["method"] == "POST" for r in sent) == 1
    assert any(r["method"] == "GET" for r in sent) == (change == "changed")
    assert query(client, lambda s: s.scalar(select(File.id))) is None
    assert query(client, lambda s: s.get(ModelInvocation, identifier)).result_context == original

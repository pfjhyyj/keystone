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
        "/api/v1/flow-runs",
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


@pytest.mark.parametrize("case", ["local", "generation"])
def test_image_failures_keep_real_cost_and_delivery_facts_separate(
    client, monkeypatch, tmp_path, case
):
    request_log = tmp_path / "requests.jsonl"
    run_id = accept(client, monkeypatch, "openlux")
    options = {"local": case == "local", "generation_error": case == "generation"}
    with worker(client, tmp_path, code=code(request_log, **options)) as (handle, log):
        done = wait_run(client, run_id, handle, log)
        assert done["status"] == ("failed" if case == "generation" else "succeeded"), (
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
    assert current.status == ("unknown" if case == "generation" else "succeeded")
    files = query(client, lambda s: s.scalars(select(File))).all()
    assert bool(files) == (case == "local")
    requests = [json.loads(line) for line in request_log.read_text().splitlines()]
    assert sum(r["method"] == "POST" for r in requests) == 1


@pytest.mark.parametrize(
    "case,reason",
    [
        ("header", "cost_request_id_missing"),
        ("model", "cost_receipt_conflict"),
        ("currency", "cost_policy_changed"),
    ],
)
def test_image_billing_conflicts_preserve_successful_delivery(
    client, monkeypatch, tmp_path, case, reason
):
    # These are billing rules, independent of DBOS checkpoints. Keep real image
    # inference, original receipts, SQL reconciliation and Files publication.
    from keystone.platform.model_access.adapters.http import HttpClient

    configured = channel(client, "openlux", api_base="https://api.openlux.ai/v1")
    entry(client, configured, provider_model="gpt-image-2", operations=["image.generate"])
    request_log = tmp_path / "requests.jsonl"
    monkeypatch.setattr(HttpClient, "transport", HttpClient.transport)
    install(request_log, header=case != "header")
    response = client.post(
        "/api/v1/models/images/generate", json={"model_key": "chat", "prompt": "A blue square"}
    )
    assert response.status_code == 200, response.text
    identifier = response.json()["invocation_id"]
    before = stored(client, identifier)
    install(
        request_log,
        model="different-model" if case == "model" else None,
        currency="CUSTOM" if case == "currency" else None,
    )
    due(client, identifier)
    process(client, identifier)
    current = stored(client, identifier)
    assert current.cost_amount is None and current.cost_check_reason == reason
    assert current.cost_check_status == ("unavailable" if case == "header" else "blocked")
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


@pytest.mark.parametrize("change", ["expired", "cipher", "disabled", "changed"])
def test_recovery_rejects_unusable_original_evidence_without_a_new_post(
    client, monkeypatch, tmp_path, change
):
    from datetime import timedelta

    from keystone.core.time import utcnow
    from keystone.platform.model_access.models import Channel

    request_log = tmp_path / "requests.jsonl"
    run_id = accept(client, monkeypatch, "yunwu")
    crash = "manifest" if change == "changed" else "sources"
    with worker(client, tmp_path, code=code(request_log, crash=crash)) as (handle, log):
        assert handle.wait(timeout=35) == 71, log.read_text()
    original = query(client, lambda s: s.scalars(select(ModelInvocation))).one().result_context

    async def update(session):
        row = await session.scalar(select(ModelInvocation))
        receipt = dict(row.result_context)
        if change == "expired":
            receipt["deadline_at"] = (utcnow() - timedelta(seconds=1)).isoformat()
        elif change == "cipher":
            receipt["sources_encrypted"] = "invalid-ciphertext"
        elif change == "disabled":
            channel = await session.get(Channel, row.channel_id)
            channel.is_active = False
        row.result_context = receipt

    query(client, update)
    with worker(
        client,
        tmp_path,
        code=code(request_log, download_format="JPEG" if change == "changed" else "PNG"),
    ) as (handle, log):
        done = wait_run(client, run_id, handle, log)
        assert done["status"] == "failed", (done, log.read_text())
    requests = [json.loads(line) for line in request_log.read_text().splitlines()]
    assert sum(r["method"] == "POST" for r in requests) == 1
    assert any(r["method"] == "GET" for r in requests) == (change == "changed")
    assert not query(client, lambda s: s.scalars(select(File))).all()
    if change == "changed":
        assert (
            query(client, lambda s: s.scalars(select(ModelInvocation))).one().result_context
            == original
        )


@pytest.mark.parametrize("revoke", [False, True])
def test_cancel_or_revoke_during_result_download_preserves_call_without_publishing(
    client, monkeypatch, tmp_path, revoke
):
    import time

    from test_identity import bearer, token

    pat = (
        token(
            client, ["flow:start", "flow:read", "model:invoke", "file:create", "file:content_read"]
        )
        if revoke
        else None
    )
    run_id = accept(client, monkeypatch, "yunwu", headers=bearer(pat["token"]) if pat else None)
    request_log = tmp_path / "requests.jsonl"
    with worker(client, tmp_path, code=code(request_log, pause=True)) as (handle, log):
        deadline = time.monotonic() + 20
        while not request_log.with_suffix(".waiting").exists():
            assert handle.poll() is None and time.monotonic() < deadline, log.read_text()
            time.sleep(0.05)
        if revoke:
            assert client.delete(f"/api/v1/auth/tokens/{pat['id']}").status_code == 204
        else:
            assert client.post(f"/api/v1/flow-runs/{run_id}/cancel").status_code == 200
        request_log.with_suffix(".release").touch()
        done = wait_run(client, run_id, handle, log)
        assert done["status"] == ("failed" if revoke else "cancelled"), (done, log.read_text())
    call = query(client, lambda s: s.scalars(select(ModelInvocation))).one()
    assert call.status == "succeeded" and call.result_context["kind"] in {"download", "images"}
    assert not query(client, lambda s: s.scalars(select(File))).all()
    requests = [json.loads(line) for line in request_log.read_text().splitlines()]
    assert sum(r["method"] == "POST" for r in requests) == 1

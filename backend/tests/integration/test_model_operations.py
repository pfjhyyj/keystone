"""Configured capabilities, fixed operations and native migration preservation."""

from uuid import UUID

import pytest
from sqlalchemy import select
from support.flow_packages import install_package
from test_files_dictionary import client as client
from test_identity import query
from test_model_access import channel, entry, invoke
from test_model_access import model_configuration as model_configuration

from keystone.platform.model_access.models import ModelInvocation

pytestmark = pytest.mark.postgres


def test_operations_are_explicit_filtered_and_preserved_on_old_client_edit(client):
    first = channel(client)
    ordinary = entry(client, first)
    assert ordinary["operations"] == ["text.generate"]
    structured = entry(client, first, key="structured", operations=["text.structured"])
    entry(client, first, key="image-only", operations=["image.generate"])
    entry(
        client,
        channel(client, provider="yunwu", api_base="https://yunwu.ai/v1"),
        key="not-implemented",
        operations=["text.structured"],
    )
    assert [r["key"] for r in client.get("/api/v1/models").json()["items"]] == ["chat"]
    result = client.get("/api/v1/models?operation=text.structured&limit=1").json()["items"]
    assert result == [{"key": "structured", "operations": ["text.structured"], "capabilities": []}]
    assert [
        r["key"] for r in client.get("/api/v1/models?operation=image.generate").json()["items"]
    ] == ["image-only"]
    assert client.get("/api/v1/models?operation=unknown").status_code == 422
    edited = client.patch(
        f"/api/v1/model-entries/{structured['id']}",
        json={
            "channel_id": first["id"],
            "provider_model": "changed",
            "is_active": True,
        },
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["operations"] == ["text.structured"]
    refused = invoke(client, key="structured")
    assert refused.status_code == 409 and refused.json()["code"] == "model_operation_unavailable"
    assert query(client, lambda s: s.scalar(select(ModelInvocation.id))) is None
    for operations in ([], ["text.generate"] * 2, ["unknown"], None):
        response = client.patch(
            f"/api/v1/model-entries/{ordinary['id']}",
            json={
                "channel_id": first["id"],
                "provider_model": "unchanged",
                "operations": operations,
            },
        )
        assert response.status_code == 422, response.text
    assert client.get("/api/v1/model-entries").json()[0]["operations"] == ["text.generate"]
    audit = client.get("/api/v1/audit-logs?action=model.entry.update").json()
    assert audit and audit[0]["details"]["after"]["operations"] == ["text.structured"]


def test_delivery_and_native_model_support_are_checked_before_admission(client, monkeypatch):
    from support.operation_flow import PACKAGE, image_definition, task_definition

    from keystone.platform.flow.models import FlowRun

    install_package(client, monkeypatch, PACKAGE, engine=False)
    entry(client, channel(client), key="sync-image", operations=["image.generate"])
    native = channel(client, provider="openlux", api_base="https://api.openlux.ai/v1")
    entry(
        client, native, key="async-image", provider_model="kling-v2", operations=["image.generate"]
    )
    entry(
        client, native, key="unsupported", provider_model="unknown", operations=["image.generate"]
    )
    for delivery, expected in (("sync", ["sync-image"]), ("async", ["async-image"])):
        rows = client.get(
            f"/api/v1/models?operation=image.generate&delivery={delivery}&limit=1"
        ).json()["items"]
        assert [row["key"] for row in rows] == expected
    for definition, binding in (
        (image_definition, "async-image"),
        (task_definition, "sync-image"),
        (task_definition, "unsupported"),
    ):
        response = client.post(
            "/api/v1/flow-runs",
            json={
                "definition_key": definition.key,
                "request_key": "rejected-" + binding,
                "input": {"prompt": "Generate"},
                "model_bindings": {"generate": binding},
            },
        )
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "model_provider_unavailable"
    assert query(client, lambda session: session.scalar(select(FlowRun.id))) is None
    assert query(client, lambda session: session.scalar(select(ModelInvocation.id))) is None


@pytest.mark.parametrize("remove_before_send", [False, True])
def test_structured_operation_routes_and_revalidates_with_real_worker(
    client,
    tmp_path,
    monkeypatch,
    remove_before_send,
):
    from support.operation_flow import PACKAGE, definition
    from test_flow import wait_run, worker

    from keystone.platform.flow.models import FlowRun

    runtime = install_package(client, monkeypatch, PACKAGE)
    runtime.flow_definitions = {definition.key: definition}
    first = channel(client)
    selected = entry(client, first, operations=["text.generate", "text.structured"])
    data = {
        "definition_key": definition.key,
        "request_key": "structured",
        "input": {"prompt": "Answer"},
        "model_bindings": {"analyze": "chat"},
    }
    response = client.post("/api/v1/flow-runs", json=data)
    assert response.status_code == 202, response.text
    run_id = response.json()["id"]
    stored = query(client, lambda s: s.get(FlowRun, UUID(run_id)))
    binding = stored.definition_snapshot["model_bindings"]["analyze"]["selection"]
    assert binding["operation"] == "text.structured"
    if remove_before_send:
        response = client.patch(
            f"/api/v1/model-entries/{selected['id']}",
            json={
                "channel_id": first["id"],
                "provider_model": selected["provider_model"],
                "operations": ["text.generate"],
            },
        )
        assert response.status_code == 200
    code = """from keystone import composition
from support.operation_flow import PACKAGE
composition.FLOW_PACKAGES = (PACKAGE,)
composition.execution_version.cache_clear()
from keystone.entrypoints.worker import main
main()
"""
    with worker(client, tmp_path, code=code) as (process, log):
        done = wait_run(client, run_id, process, log)
        assert done["status"] == ("failed" if remove_before_send else "succeeded"), (
            done,
            log.read_text(),
        )
    if remove_before_send:
        assert done["error_code"] == "model_configuration_changed"
        assert query(client, lambda s: s.scalar(select(ModelInvocation.id))) is None
        return
    assert done["result"]["answer"] == 7
    call = query(client, lambda s: s.get(ModelInvocation, UUID(done["result"]["invocation_id"])))
    assert call.model_entry_id == UUID(selected["id"]) and call.status == "succeeded"
    assert call.flow_context["model_role"] == "analyze"
    update = client.patch(
        f"/api/v1/model-entries/{selected['id']}",
        json={
            "channel_id": first["id"],
            "provider_model": selected["provider_model"],
            "operations": ["text.generate"],
        },
    )
    assert update.status_code == 200
    assert client.post("/api/v1/flow-runs", json=data).json()["id"] == run_id
    refused = client.post("/api/v1/flow-runs", json={**data, "request_key": "new"})
    assert refused.status_code == 409
    assert len(query(client, lambda s: s.scalars(select(FlowRun))).all()) == 1


@pytest.mark.parametrize("case", ["normal", "unregistered", "mislabeled", "truncated"])
def test_vision_uses_only_authorized_valid_run_files(client, tmp_path, monkeypatch, case):
    from io import BytesIO

    from PIL import Image
    from support.operation_flow import PACKAGE, vision_definition
    from test_flow import wait_run, worker

    install_package(client, monkeypatch, PACKAGE)
    entry(client, channel(client), operations=["image.understand"])
    image = BytesIO()
    Image.new("RGB", (2, 2), "red").save(image, format="PNG")
    body = image.getvalue()[:20] if case == "truncated" else image.getvalue()

    def upload_image(name):
        from test_files_dictionary import upload

        response = upload(
            client,
            body,
            key=name,
            name=name,
            content_type="text/plain" if case == "mislabeled" else "image/png",
        )
        assert response.status_code == 201, response.text
        return response.json()["id"]

    file_id = upload_image("input.png")
    inputs = {"prompt": "Describe this image", "source_file_id": file_id}
    if case == "unregistered":
        inputs["image_file_id"] = upload_image("outside.png")
    response = client.post(
        "/api/v1/flow-runs",
        json={
            "definition_key": vision_definition.key,
            "request_key": "vision",
            "input": inputs,
            "model_bindings": {"analyze": "chat"},
        },
    )
    assert response.status_code == 202, response.text
    code = """from keystone import composition
from support.operation_flow import PACKAGE
composition.FLOW_PACKAGES = (PACKAGE,)
composition.execution_version.cache_clear()
from keystone.entrypoints.worker import main
main()
"""
    with worker(client, tmp_path, code=code) as (process, log):
        done = wait_run(client, response.json()["id"], process, log)
        assert done["status"] == ("succeeded" if case == "normal" else "failed"), (
            done,
            log.read_text(),
        )
    calls = query(client, lambda s: s.scalars(select(ModelInvocation))).all()
    assert len(calls) == (1 if case == "normal" else 0)
    if case != "normal":
        expected = "flow_file_scope_denied" if case == "unregistered" else "model_invalid_image"
        assert done["error_code"] == expected
    else:
        assert calls[0].status == "succeeded"
        assert "data:image" not in str(done)


def test_native_structured_agent_uses_its_declared_operation(client, monkeypatch, tmp_path):
    from support.operation_flow import PACKAGE, agent_definition
    from test_flow import wait_run, worker

    install_package(client, monkeypatch, PACKAGE)
    entry(client, channel(client), operations=["text.structured"])
    response = client.post(
        "/api/v1/flow-runs",
        json={
            "definition_key": agent_definition.key,
            "request_key": "native",
            "input": {"prompt": "Answer"},
            "model_bindings": {"analyze": "chat"},
        },
    )
    assert response.status_code == 202, response.text
    code = """from keystone import composition
from support.operation_flow import PACKAGE
composition.FLOW_PACKAGES = (PACKAGE,)
composition.execution_version.cache_clear()
from keystone.entrypoints.worker import main
main()
"""
    with worker(client, tmp_path, code=code) as (process, log):
        done = wait_run(client, response.json()["id"], process, log)
        assert done["status"] == "succeeded", (done, log.read_text())
    assert done["result"] == {"answer": 7}
    calls = query(client, lambda s: s.scalars(select(ModelInvocation))).all()
    assert len(calls) == 1 and calls[0].operation == "text.structured"
    assert (
        calls[0].flow_context["agent_call_id"] and calls[0].flow_context["model_role"] == "analyze"
    )

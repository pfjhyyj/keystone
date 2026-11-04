"""Rename and conditional deletion keep invocation and Flow selections intact."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event
from test_files_dictionary import client as client
from test_identity import bearer, query, token
from test_model_access import channel, edit_channel, entry, invoke
from test_model_access import model_configuration as model_configuration

from keystone.core.time import utcnow
from keystone.platform.flow.models import FlowRun
from keystone.platform.model_access.models import Channel

pytestmark = pytest.mark.postgres


def test_unused_models_and_channels_delete_without_exposing_secrets(client):
    c = channel(client)
    e = entry(client, c)
    assert e["name"] == e["key"]
    assert (
        client.delete(f"/api/v1/model-channels/{c['id']}").json()["code"]
        == "model_channel_referenced"
    )
    assert client.delete(f"/api/v1/model-entries/{e['id']}").status_code == 204
    assert client.delete(f"/api/v1/model-channels/{c['id']}").status_code == 204
    assert client.get("/api/v1/model-entries").json() == []
    assert client.get("/api/v1/model-channels").json() == []
    assert client.delete(f"/api/v1/model-entries/{e['id']}").status_code == 404
    logs = client.get("/api/v1/audit-logs?action=model.channel.delete").json()
    assert logs[0]["outcome"] == "success"
    assert set(logs[0]["details"]) == {"name", "provider"}
    c = channel(client)
    assert entry(client, c, key=e["key"])["key"] == e["key"]


@pytest.mark.parametrize("kind", ["channel", "entry"])
def test_manage_permission_required_for_delete_and_quick_rename(client, kind):
    c = channel(client)
    e = entry(client, c)
    row = c if kind == "channel" else e
    path = f"/api/v1/model-{'channels' if kind == 'channel' else 'entries'}/{row['id']}"
    limited = bearer(token(client, ["model:invoke"])["token"])
    assert client.delete(path, headers=limited).status_code == 403
    assert (
        client.patch(path + "/name", headers=limited, json={"name": "forbidden"}).status_code == 403
    )
    assert client.patch(path + "/name", json={"name": "   "}).status_code == 422


def test_rename_keeps_original_mapping_credentials_and_call_history(client):
    c = channel(client)
    e = entry(client, c)
    call = invoke(client).json()["invocation_id"]
    old_channel = query(client, lambda s: s.get(Channel, UUID(c["id"])))
    changed = client.patch(f"/api/v1/model-entries/{e['id']}/name", json={"name": "常用文本模型"})
    assert changed.status_code == 200
    assert changed.json()["key"] == "chat" and changed.json()["name"] == "常用文本模型"
    assert (
        client.patch(f"/api/v1/model-channels/{c['id']}/name", json={"name": "主渠道"}).status_code
        == 200
    )
    new_channel = query(client, lambda s: s.get(Channel, UUID(c["id"])))
    for field in (
        "api_key_encrypted",
        "billing_api_key_encrypted",
        "cost_context",
        "api_base",
        "provider",
    ):
        assert getattr(old_channel, field) == getattr(new_channel, field)
    assert client.get("/api/v1/_test/model-options").json()["items"][0]["name"] == "常用文本模型"
    assert client.get(f"/api/v1/model-invocations/{call}").json()["model_entry_id"] == e["id"]
    for path in (f"model-entries/{e['id']}", f"model-channels/{c['id']}"):
        response = client.delete("/api/v1/" + path)
        assert response.status_code == 409 and "只能停用" in response.json()["detail"]
    assert edit_channel(client, c, is_active=False).status_code == 200


@pytest.mark.parametrize("terminal", [False, True])
def test_flow_reference_blocks_delete_before_any_call_including_original_channel(
    client, monkeypatch, terminal
):
    from support.flow_packages import install_package
    from support.operation_flow import PACKAGE, definition

    install_package(client, monkeypatch, PACKAGE, engine=False)
    c = channel(client)
    e = entry(client, c, operations=["text.generate", "text.structured"])
    result = client.post(
        "/api/v1/_test/review",
        json={
            "definition_key": definition.key,
            "request_key": str(uuid4()),
            "input": {"prompt": "test"},
            "model_bindings": {"analyze": e["key"]},
        },
    )
    assert result.status_code == 202, result.text
    run_id = UUID(result.json()["id"])
    if terminal:

        async def finish(session):
            run = await session.get(FlowRun, run_id)
            run.status, run.finished_at = "failed", utcnow()

        query(client, finish)
    snapshot = query(client, lambda s: s.get(FlowRun, run_id)).definition_snapshot
    assert client.get("/api/v1/model-invocations").json() == []
    assert (
        client.patch(f"/api/v1/model-entries/{e['id']}/name", json={"name": "新名称"}).status_code
        == 200
    )
    assert (
        client.delete(f"/api/v1/model-entries/{e['id']}").json()["code"] == "model_entry_referenced"
    )
    assert query(client, lambda s: s.get(FlowRun, run_id)).definition_snapshot == snapshot
    second = channel(client)
    assert (
        client.patch(
            f"/api/v1/model-entries/{e['id']}",
            json={
                "channel_id": second["id"],
                "provider_model": e["provider_model"],
            },
        ).status_code
        == 200
    )
    assert (
        client.delete(f"/api/v1/model-channels/{c['id']}").json()["code"]
        == "model_channel_referenced"
    )


def test_delete_rolls_back_if_audit_fails(client, monkeypatch):
    from keystone.platform.audit.services import writer

    c = channel(client)
    original = writer.record

    async def fail(session, action, *args, **kwargs):
        if action == "model.channel.delete" and kwargs.get("outcome", "success") == "success":
            raise RuntimeError("audit unavailable")
        return await original(session, action, *args, **kwargs)

    monkeypatch.setattr(writer, "record", fail)
    assert client.delete(f"/api/v1/model-channels/{c['id']}").status_code == 500
    assert client.get("/api/v1/model-channels").json()[0]["id"] == c["id"]


@pytest.mark.parametrize("binding_first", [True, False])
def test_binding_and_deletion_serialize_on_the_same_entry_lock(client, monkeypatch, binding_first):
    from support.flow_packages import install_package
    from support.operation_flow import PACKAGE, definition

    from keystone.platform.flow.services import runs

    install_package(client, monkeypatch, PACKAGE, engine=False)
    c = channel(client)
    e = entry(client, c, operations=["text.generate", "text.structured"])
    entered, release, competing = Event(), Event(), Event()
    runtime = client.app.state.runtime
    original = (
        runs.resolve_bindings_in_transaction if binding_first else runtime.models.model_referenced
    )

    async def hold(*args, **kwargs):
        if binding_first:
            result = await original(*args, **kwargs)
        entered.set()
        await asyncio.to_thread(release.wait, 10)
        return result if binding_first else await original(*args, **kwargs)

    if binding_first:
        monkeypatch.setattr(runs, "resolve_bindings_in_transaction", hold)
    else:
        monkeypatch.setattr(runtime, "models", replace(runtime.models, model_referenced=hold))

    def executing(conn, cursor, statement, parameters, context, many):
        if (
            "model_entries" in statement
            and ("FOR UPDATE" if binding_first else "FOR SHARE") in statement
        ):
            competing.set()

    def start():
        return client.post(
            "/api/v1/_test/review",
            json={
                "definition_key": definition.key,
                "request_key": str(uuid4()),
                "input": {"prompt": "race"},
                "model_bindings": {"analyze": e["key"]},
            },
        )

    def delete():
        return client.delete(f"/api/v1/model-entries/{e['id']}")

    engine = runtime.database.engine.sync_engine
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(start if binding_first else delete)
        try:
            assert entered.wait(10)
            event.listen(engine, "before_cursor_execute", executing)
            second = pool.submit(delete if binding_first else start)
            assert competing.wait(10)
            assert not second.done()
        finally:
            release.set()
        first_result, second_result = first.result(15), second.result(15)
        event.remove(engine, "before_cursor_execute", executing)
    assert first_result.status_code == (202 if binding_first else 204)
    assert second_result.status_code == 409
    assert bool(client.get("/api/v1/model-entries").json()) is binding_first

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Event
from uuid import UUID, uuid4

import httpx2
import model_stub as mock
import pytest
from pydantic import SecretStr
from sqlalchemy import event, update
from test_files_dictionary import client as client
from test_identity import PASSWORD, assign, bearer, create_user, login, query, role, token

from keystone.core.errors import ServiceError
from keystone.entrypoints import cli
from keystone.platform.audit.services import writer as audit
from keystone.platform.identity.access import AccessContext
from keystone.platform.identity.policy import Credential
from keystone.platform.model_access import schemas
from keystone.platform.model_access.models import ModelInvocation
from keystone.platform.model_access.services.entries import ModelEntryService
from keystone.platform.model_access.services.maintenance import recover_api_calls
from keystone.platform.model_access.services.runtime import ModelRuntime

pytestmark = pytest.mark.postgres
SECRET = "test-only-inference-secret"
BILLING = "test-only-billing-secret"


def test_default_history_uses_database_clock(client, monkeypatch):
    from keystone.core import time as clock

    entry(client, channel(client))
    identifier = invoke(client).json()["invocation_id"]

    class SlowClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) - timedelta(hours=2)

    monkeypatch.setattr(clock, "datetime", SlowClock)
    assert [r["id"] for r in client.get("/api/v1/model-invocations").json()] == [identifier]
    assert client.get("/api/v1/model-invocations/summary").json()["attempts_by_status"] == {
        "succeeded": 1
    }
    assert client.get("/api/v1/audit-logs?action=model.entry.create").json()


def test_history_windows_match_audit_and_model_queries(client):
    from keystone.platform.audit.models import AuditLog

    entry(client, channel(client))
    identifiers = [invoke(client).json()["invocation_id"] for _ in range(4)]
    until = datetime(2025, 1, 31, tzinfo=UTC)
    since = until - timedelta(days=30)
    dates = [since - timedelta(seconds=1), since, until, until + timedelta(seconds=1)]

    async def prepare(session):
        for identifier, timestamp in zip(identifiers, dates, strict=True):
            await session.execute(
                update(ModelInvocation)
                .where(ModelInvocation.id == UUID(identifier))
                .values(created_at=timestamp)
            )
            session.add(
                AuditLog(
                    occurred_at=timestamp,
                    actor_type="system",
                    action="model.invocation.recover",
                    target_id=identifier,
                    outcome="success",
                )
            )

    query(client, prepare)
    for bounds in (
        {"until": until.isoformat()},
        {"since": since.isoformat(), "until": until.isoformat()},
    ):
        rows = client.get("/api/v1/model-invocations", params=bounds).json()
        assert {r["id"] for r in rows} == set(identifiers[1:3])
        logs = client.get(
            "/api/v1/audit-logs", params={**bounds, "action": "model.invocation.recover"}
        ).json()
        assert {r["target_id"] for r in logs} == set(identifiers[1:3])
        assert client.get("/api/v1/model-invocations/summary", params=bounds).json()[
            "attempts_by_status"
        ] == {"succeeded": 2}
    for bounds in (
        {"since": "2025-01-01", "until": until.isoformat()},
        {"since": until.isoformat(), "until": since.isoformat()},
        {"since": "2020-01-01T00:00:00Z", "until": until.isoformat()},
    ):
        for path in ("audit-logs", "model-invocations", "model-invocations/summary"):
            assert client.get(f"/api/v1/{path}", params=bounds).status_code == 422


def test_offline_api_recovery_preserves_receipts_and_excludes_flow(client, monkeypatch):
    entry(client, channel(client))
    identifiers = [invoke(client).json()["invocation_id"] for _ in range(3)]
    original = client.get(f"/api/v1/model-invocations/{identifiers[0]}").json()

    async def interrupted(session):
        await session.execute(
            update(ModelInvocation)
            .where(ModelInvocation.id.in_([UUID(v) for v in identifiers[:2]]))
            .values(status="running", finished_at=None)
        )
        await session.execute(
            update(ModelInvocation)
            .where(ModelInvocation.id == UUID(identifiers[1]))
            .values(flow_context={"flow_run_id": str(uuid4())})
        )

    query(client, interrupted)
    assert client.post(f"/api/v1/model-invocations/{identifiers[0]}/cost").status_code == 409
    # Simulate records left by stopped processes. No model requests are in flight.
    with pytest.raises(ServiceError) as rejected:
        client.portal.call(recover_api_calls, client.app.state.runtime.database)
    assert rejected.value.code == "api_stop_required"
    monkeypatch.setattr(cli, "Settings", lambda: client.app.state.runtime.settings)
    monkeypatch.setattr("sys.argv", ["keystone", "recover-model-calls", "--confirm-api-stopped"])
    cli.main()
    recovered = client.get(f"/api/v1/model-invocations/{identifiers[0]}").json()
    assert recovered["status"] == "unknown" and recovered["error_code"] == "model_interrupted"
    assert recovered["finished_at"]
    for field in (
        "upstream_request_id",
        "usage",
        "cost_amount",
        "cost_unit",
        "cost_source",
        "created_at",
    ):
        assert recovered[field] == original[field]
    assert client.get(f"/api/v1/model-invocations/{identifiers[1]}").json()["status"] == "running"
    assert client.get(f"/api/v1/model-invocations/{identifiers[2]}").json()["status"] == "succeeded"
    cli.main()  # Idempotent, including audit; no new inference attempts.
    logs = client.get("/api/v1/audit-logs", params={"action": "model.invocation.recover"}).json()
    assert len(logs) == 1 and logs[0]["target_id"] == identifiers[0]
    assert logs[0]["actor_type"] == "system" and logs[0]["actor_id"] is None
    assert logs[0]["details"] == {"source": "offline_api_recovery"}
    assert len(client.get("/api/v1/model-invocations").json()) == 3
    assert client.post(f"/api/v1/model-invocations/{identifiers[0]}/cost").status_code == 200


def test_offline_recovery_audit_failure_rolls_back_batch(client, monkeypatch):
    entry(client, channel(client))
    identifiers = [invoke(client).json()["invocation_id"] for _ in range(2)]
    query(
        client,
        lambda session: session.execute(
            update(ModelInvocation).values(status="running", finished_at=None)
        ),
    )
    original_record = audit.record
    recorded = 0

    async def fail_second(*args, **kwargs):
        nonlocal recorded
        if args[1] == "model.invocation.recover":
            recorded += 1
            if recorded == 2:
                raise RuntimeError("test audit failure")
        return await original_record(*args, **kwargs)

    monkeypatch.setattr(audit, "record", fail_second)

    async def recover():
        return await recover_api_calls(client.app.state.runtime.database, api_stopped=True)

    with pytest.raises(RuntimeError, match="test audit failure"):
        client.portal.call(recover)
    for identifier in identifiers:
        row = client.get(f"/api/v1/model-invocations/{identifier}").json()
        assert row["status"] == "running" and row["finished_at"] is None
    assert (
        client.get("/api/v1/audit-logs", params={"action": "model.invocation.recover"}).json() == []
    )


@pytest.fixture(autouse=True)
def model_configuration(client, monkeypatch):
    mock.install(monkeypatch)
    settings = client.app.state.runtime.settings
    settings.model_secrets = {
        "inference": SecretStr(SECRET),
        "billing": SecretStr(BILLING),
        "rotated": SecretStr("rotated-test-secret"),
    }


def channel(client, provider="openrouter", **overrides):
    response = client.post(
        "/api/v1/model-channels",
        json={
            "name": provider,
            "provider": provider,
            "api_base": "https://openrouter.ai/api/v1",
            "credential_ref": "inference",
            "billing_credential_ref": "billing",
            **overrides,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def entry(client, channel, key="chat", **overrides):
    response = client.post(
        "/api/v1/model-entries",
        json={"key": key, "channel_id": channel["id"], "provider_model": "mock-chat", **overrides},
    )
    assert response.status_code == 201, response.text
    return response.json()


def invoke(client, key="chat", stream=False, **overrides):
    return client.post(
        "/api/v1/models/invoke",
        json={
            "model_key": key,
            "messages": [{"role": "user", "content": "private-prompt-must-not-persist"}],
            "stream": stream,
            **overrides,
        },
    )


def edit_channel(client, row, **changes):
    fields = (
        "name",
        "api_base",
        "credential_ref",
        "billing_credential_ref",
        "upstream_account_ref",
        "is_active",
    )
    return client.patch(
        f"/api/v1/model-channels/{row['id']}", json={**{k: row[k] for k in fields}, **changes}
    )


def internal_service(client, secret):
    runtime = client.app.state.runtime
    return ModelEntryService(
        AccessContext(
            runtime.database,
            runtime.settings,
            Credential("pat", secret),
        ),
        runtime.models,
    )


def install_transport(client, monkeypatch, handler):
    monkeypatch.setattr(
        client.app.state.runtime.models,
        "transport",
        lambda provider: httpx2.MockTransport(lambda request: handler(provider, request)),
    )


@pytest.mark.parametrize(
    "provider,unit,cost",
    [
        ("deepseek", None, None),
        ("openrouter", "credits", Decimal("0.0012")),
        ("aliyun_bailian", None, None),
    ],
)
def test_sdk_nonstream_and_stream_receipts_no_messages_persist(client, provider, unit, cost):
    entry(client, channel(client, provider))
    a = invoke(client)
    assert a.status_code == 200, a.text
    assert "mode" not in a.json()
    b = invoke(client, stream=True)
    assert b.status_code == 200
    events = [json.loads(line[6:]) for line in b.text.splitlines() if line.startswith("data: ")]
    assert [e["type"] for e in events] == ["start", "delta", "done"], b.text
    records = client.get("/api/v1/model-invocations").json()
    assert len(records) == 2
    for row in records:
        assert row["status"] == "succeeded" and row["finished_at"] and row["upstream_request_id"]
        assert row["usage"] == {"input_tokens": 12, "output_tokens": 8, "total_tokens": 20}
        assert (Decimal(row["cost_amount"]) if row["cost_amount"] else None) == cost
        assert row["cost_unit"] == unit
        assert not {"messages", "content", "credential_ref", "api_base"} & row.keys()
    assert "private-prompt" not in json.dumps(records)
    summary = client.get("/api/v1/model-invocations/summary").json()
    assert summary["attempts_by_status"] == {"succeeded": 2}
    assert summary["unknown_cost_attempts"] == (0 if unit else 2)


def test_no_retry_actual_send_registered_and_secret_only_on_transport(client, monkeypatch):
    entry(client, channel(client))
    sends = []

    async def respond(provider, request):
        sends.append(request)
        identifier = UUID(request.headers["x-keystone-invocation-id"])
        async with client.app.state.runtime.database.transaction() as session:
            row = await session.get(ModelInvocation, identifier)
            assert row is not None and row.status == "running"  # committed before HTTP send
        assert request.headers["authorization"] == "Bearer " + SECRET
        return httpx2.Response(500, json={"error": {"message": SECRET + " private-prompt"}})

    install_transport(client, monkeypatch, respond)
    r = invoke(client)
    assert r.status_code == 502 and len(sends) == 1
    assert SECRET not in r.text and "private-prompt" not in r.text
    row = client.get("/api/v1/model-invocations").json()[0]
    assert row["status"] == "unknown" and row["cost_amount"] is None
    assert row["error_code"] == "model_upstream_error"


def test_stable_id_concurrent_claim_and_missing_checkpoint_never_resends(client, monkeypatch):
    entry(client, channel(client))
    pat = token(client, ["model:invoke"])["token"]
    runtime = ModelRuntime(internal_service(client, pat))
    data = schemas.Invoke(model_key="chat", messages=[{"role": "user", "content": "test"}])
    sends = []

    async def respond(provider, request):
        sends.append(1)
        return await mock.transport(provider).handle_async_request(request)

    install_transport(client, monkeypatch, respond)
    identifier = uuid4()

    async def competition():
        results = await asyncio.gather(
            runtime.invoke(data, invocation_id=identifier),
            runtime.invoke(data, invocation_id=identifier),
            return_exceptions=True,
        )
        assert (
            sum(
                isinstance(r, ServiceError) and r.code == "model_result_unavailable"
                for r in results
            )
            == 1
        )
        with pytest.raises(ServiceError) as replay:
            await runtime.invoke(data, invocation_id=identifier)
        assert replay.value.code == "model_result_unavailable"

    client.portal.call(competition)
    assert len(sends) == 1
    assert client.get(f"/api/v1/model-invocations/{identifier}").json()["status"] == "succeeded"
    assert invoke(client, invocation_id=str(uuid4())).status_code == 422
    assert invoke(client, flow_context={"flow_run_id": str(uuid4())}).status_code == 422


def test_timeout_and_incomplete_stream_remain_unknown(client, monkeypatch):
    entry(client, channel(client))

    async def timeout(provider, request):
        raise httpx2.ReadTimeout("unsafe secret " + SECRET, request=request)

    install_transport(client, monkeypatch, timeout)
    assert invoke(client).status_code == 504

    async def interrupted(provider, request):
        event = {
            "id": "safe-id",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "mock-chat",
            "choices": [{"index": 0, "delta": {"content": "partial"}, "finish_reason": None}],
        }
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=mock.MockStream([("data: " + json.dumps(event) + "\n\n").encode()]),
        )

    install_transport(client, monkeypatch, interrupted)
    streamed = invoke(client, stream=True)
    assert "model_stream_incomplete" in streamed.text and '"type": "done"' not in streamed.text
    rows = client.get("/api/v1/model-invocations").json()
    assert {r["status"] for r in rows} == {"unknown"}
    assert all(r["usage"] is None and r["cost_amount"] is None for r in rows)


def test_zero_cost_decimal_precision_and_byok_not_substituted(client, monkeypatch):
    entry(client, channel(client))
    costs = ["0", "0.000000000123456789", None, "99999999999999999999.123456789123456789"]

    async def respond(provider, request):
        cost = costs.pop(0)
        raw = '{"id":"test-id","object":"chat.completion","created":0,"model":"mock-chat",'
        raw += '"choices":[{"index":0,"message":{"role":"assistant","content":"ok"},'
        raw += '"finish_reason":"stop"}],'
        raw += '"usage":{"prompt_tokens":10,"upstream_inference_cost":99'
        if cost is not None:
            raw += ',"cost":' + cost
        raw += "}}"
        return httpx2.Response(200, content=raw, headers={"content-type": "application/json"})

    install_transport(client, monkeypatch, respond)
    for _ in range(3):
        assert invoke(client).status_code == 200
    rows = client.get("/api/v1/model-invocations").json()
    values = {Decimal(r["cost_amount"]) if r["cost_amount"] is not None else None for r in rows}
    assert values == {None, Decimal("0"), Decimal("0.000000000123456789")}
    summary = client.get("/api/v1/model-invocations/summary").json()
    assert summary["unknown_cost_attempts"] == 1
    assert Decimal(summary["known_costs"][0]["amount"]) == Decimal("0.000000000123456789")
    assert invoke(client).status_code == 200
    summary = client.get("/api/v1/model-invocations/summary").json()
    assert summary["known_costs"][0]["amount"] == "99999999999999999999.123456789246913578"

    async def credits(provider, request):
        return httpx2.Response(
            200,
            json={
                "data": {
                    "total_credits": "99999999999999999999.123456789123456789",
                    "total_usage": "0.000000000000000001",
                }
            },
        )

    install_transport(client, monkeypatch, credits)
    identifier = client.get("/api/v1/model-channels").json()[0]["id"]
    balance = client.post(f"/api/v1/model-channels/{identifier}/balance").json()
    assert balance["balance_snapshot"]["items"][0]["amount"] == (
        "99999999999999999999.123456789123456788"
    )


def test_disabled_channel_models_independent_and_snapshot_mapping(client):
    c = channel(client)
    e = entry(client, c)
    pat = token(client, ["model:invoke"])["token"]
    service = internal_service(client, pat)
    selected = client.portal.call(service.selection, "chat")
    second = channel(client, "deepseek")
    assert (
        client.patch(
            f"/api/v1/model-entries/{e['id']}",
            json={"channel_id": second["id"], "provider_model": "changed-model", "is_active": True},
        ).status_code
        == 200
    )
    data = schemas.Invoke(model_key="chat", messages=[{"role": "user", "content": "test"}])

    async def frozen():
        return await ModelRuntime(service).invoke(data, selection=selected)

    result = client.portal.call(frozen)
    row = client.get("/api/v1/model-invocations/" + result["invocation_id"]).json()
    assert row["channel_id"] == c["id"] and row["provider_model"] == "mock-chat"
    edit_channel(client, c, is_active=False)
    with pytest.raises(ServiceError):
        client.portal.call(frozen)
    assert invoke(client).status_code == 200  # Current mapping uses the second channel.
    edit_channel(client, second, is_active=False)
    assert invoke(client).status_code == 409 and client.get("/api/v1/models").json()["items"] == []
    assert client.get("/api/v1/model-entries").json()[0]["is_active"] is True
    assert client.delete("/api/v1/model-channels/" + c["id"]).status_code == 405
    assert client.delete("/api/v1/model-entries/" + e["id"]).status_code == 405


def test_balance_refresh_capabilities_secret_selection_and_failed_snapshot(client, monkeypatch):
    c = channel(client)
    called = []

    async def respond(provider, request):
        called.append(request.url.path)
        assert request.headers["authorization"] == "Bearer " + BILLING
        return await mock.transport(provider).handle_async_request(request)

    install_transport(client, monkeypatch, respond)
    first = client.post(f"/api/v1/model-channels/{c['id']}/balance").json()
    assert first["balance_check_status"] == "ok", first
    assert first["balance_snapshot"]["items"] == [
        {"kind": "remaining", "amount": "87.50", "unit": "credits"}
    ]
    assert first["updated_at"] == c["updated_at"]

    async def fail(provider, request):
        return httpx2.Response(503)

    install_transport(client, monkeypatch, fail)
    failed = client.post(f"/api/v1/model-channels/{c['id']}/balance").json()
    assert (
        failed["balance_snapshot"] == first["balance_snapshot"]
        and failed["balance_check_status"] == "error"
    )
    assert failed["balance_checked_at"] == first["balance_checked_at"]
    renamed = edit_channel(client, c, name="renamed", is_active=False).json()
    assert renamed["balance_snapshot"] == first["balance_snapshot"]
    reset = edit_channel(client, renamed, billing_credential_ref="rotated").json()
    assert reset["balance_snapshot"] is None and reset["balance_check_status"] == "never"
    no_billing = channel(client, "aliyun_bailian", billing_credential_ref=None)
    r = client.post(f"/api/v1/model-channels/{no_billing['id']}/balance").json()
    assert r["balance_check_status"] == "unavailable" and r["balance_snapshot"] is None


def test_balance_config_change_discards_inflight_result_and_serializes_refresh(client, monkeypatch):
    c = channel(client)
    pat = token(client, ["model:manage"])
    headers = bearer(pat["token"])
    entered, release = Event(), Event()

    async def respond(provider, request):
        entered.set()
        await asyncio.to_thread(release.wait, 10)
        return await mock.transport(provider).handle_async_request(request)

    install_transport(client, monkeypatch, respond)
    path = f"/api/v1/model-channels/{c['id']}/balance"
    with ThreadPoolExecutor() as pool:
        future = pool.submit(client.post, path, headers=headers)
        try:
            assert entered.wait(5)
            assert client.post(path, headers=headers).status_code == 409
            assert edit_channel(client, c, billing_credential_ref="rotated").status_code == 200
        finally:
            release.set()
        assert future.result().json()["code"] == "model_configuration_changed"
    row = client.get("/api/v1/model-channels").json()[0]
    assert row["balance_snapshot"] is None and row["balance_check_status"] == "never"
    audits = client.get("/api/v1/audit-logs?action=model.balance.refresh").json()
    assert {a["details"]["reason"] for a in audits} == {
        "model_refresh_busy",
        "model_configuration_changed",
    }
    assert all(a["details"]["credential_id"] == pat["id"] for a in audits)


def test_cost_lookup_replaces_never_adds_and_preserves_known_on_failure(client, monkeypatch):
    entry(client, channel(client))
    result = invoke(client).json()
    path = "/api/v1/model-invocations/" + result["invocation_id"] + "/cost"
    assert client.post(path).json()["cost_source"] == "query"
    assert Decimal(client.post(path).json()["cost_amount"]) == Decimal("0.0012")

    async def fail(provider, request):
        return httpx2.Response(500)

    install_transport(client, monkeypatch, fail)
    row = client.post(path).json()
    assert row["status"] == "succeeded" and row["cost_source"] == "query"
    assert Decimal(row["cost_amount"]) == Decimal("0.0012")
    assert len(client.get("/api/v1/model-invocations").json()) == 1


def test_config_audit_rollback_secret_refs_and_immutable_identity(client, monkeypatch):
    c = channel(client, upstream_account_ref="account-one")
    assert edit_channel(client, c, upstream_account_ref="other").status_code == 409
    assert edit_channel(client, c, api_base="https://name:secret@example.com/v1").status_code == 422
    assert edit_channel(client, c, credential_ref="unknown").status_code == 422
    assert edit_channel(client, c, provider="deepseek").status_code == 422
    original = audit.record

    async def fail(session, action, *args, **kwargs):
        if action == "model.channel.update" and kwargs.get("outcome", "success") == "success":
            raise RuntimeError("audit unavailable")
        return await original(session, action, *args, **kwargs)

    monkeypatch.setattr(audit, "record", fail)
    assert edit_channel(client, c, name="must rollback").status_code == 500
    rows = client.get("/api/v1/model-channels").text
    assert "must rollback" not in rows and SECRET not in rows and BILLING not in rows
    assert SECRET not in client.get("/api/v1/audit-logs").text


def test_history_self_all_and_pat_permissions_are_independent(client):
    entry(client, channel(client))
    admin_call = invoke(client).json()["invocation_id"]
    alice = create_user(client)
    r = role(client, "model-user", ["model:invoke", "model_invocation:read"])
    assign(client, alice["id"], r["id"])
    login(client, "alice")
    client.post(
        "/api/v1/auth/password",
        json={"current_password": PASSWORD, "password": "changed-password-123"},
    )
    login(client, "alice", "changed-password-123")
    call = invoke(client).json()["invocation_id"]
    assert client.get("/api/v1/model-invocations/" + admin_call).status_code == 404
    assert len(client.get("/api/v1/model-invocations").json()) == 1
    assert client.get("/api/v1/model-channels").status_code == 403
    read_pat = token(client, ["model_invocation:read"])["token"]
    assert (
        client.get("/api/v1/model-invocations/" + call, headers=bearer(read_pat)).status_code == 200
    )
    invoke_pat = token(client, ["model:invoke"])["token"]
    assert client.get("/api/v1/model-invocations", headers=bearer(invoke_pat)).status_code == 403
    assert (
        client.get("/api/v1/model-invocations/summary", headers=bearer(invoke_pat)).status_code
        == 403
    )
    login(client)
    all_only = token(client, ["model_invocation:read_all"])["token"]
    assert client.get("/api/v1/model-invocations", headers=bearer(all_only)).status_code == 403
    manage_only = token(client, ["model:manage"])["token"]
    assert client.get("/api/v1/models", headers=bearer(manage_only)).status_code == 403
    assert len(client.get("/api/v1/model-invocations").json()) == 2


def test_inflight_completion_after_user_revocation_and_channel_disable(client, monkeypatch):
    c = channel(client)
    entry(client, c)
    pat = token(client, ["model:invoke"])["token"]
    entered, release = Event(), Event()

    async def respond(provider, request):
        entered.set()
        await asyncio.to_thread(release.wait, 10)
        return await mock.transport(provider).handle_async_request(request)

    install_transport(client, monkeypatch, respond)
    payload = {"model_key": "chat", "messages": [{"role": "user", "content": "test"}]}
    with ThreadPoolExecutor() as pool:
        pending = pool.submit(
            client.post, "/api/v1/models/invoke", json=payload, headers=bearer(pat)
        )
        try:
            assert entered.wait(5)
            tokens = client.get("/api/v1/auth/tokens").json()
            assert client.delete("/api/v1/auth/tokens/" + tokens[0]["id"]).status_code == 204
            assert edit_channel(client, c, is_active=False).status_code == 200
        finally:
            release.set()
        assert pending.result().status_code == 200
    row = client.get("/api/v1/model-invocations").json()[0]
    assert row["status"] == "succeeded" and row["cost_amount"] is not None
    assert (
        client.post("/api/v1/models/invoke", json=payload, headers=bearer(pat)).status_code == 401
    )


def test_missing_secret_never_sends(client):
    entry(client, channel(client))
    client.app.state.runtime.settings.model_secrets = {}
    assert invoke(client).json()["code"] == "model_credential_unavailable"
    assert client.get("/api/v1/model-invocations").json() == []


@pytest.mark.parametrize("failure_phase", ["register", "complete"])
def test_database_failure_never_causes_model_resend(client, monkeypatch, failure_phase):
    entry(client, channel(client))
    sends = []

    async def respond(provider, request):
        sends.append(1)
        return await mock.transport(provider).handle_async_request(request)

    install_transport(client, monkeypatch, respond)
    engine = client.app.state.runtime.database.engine.sync_engine

    def fail(connection, cursor, statement, parameters, context, executemany):
        needle = (
            "INSERT INTO keystone.model_invocations"
            if failure_phase == "register"
            else "UPDATE keystone.model_invocations"
        )
        if needle in statement:
            raise RuntimeError("database unavailable with sensitive details")

    pat = token(client, ["model:invoke"])["token"]
    runtime = ModelRuntime(internal_service(client, pat))
    identifier = uuid4()
    data = schemas.Invoke(model_key="chat", messages=[{"role": "user", "content": "test"}])

    async def call():
        return await runtime.invoke(data, invocation_id=identifier)

    event.listen(engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(ServiceError):
            client.portal.call(call)
    finally:
        event.remove(engine, "before_cursor_execute", fail)
    if failure_phase == "register":
        assert sends == [] and client.get("/api/v1/model-invocations").json() == []
    else:
        assert sends == [1]
        assert client.get("/api/v1/model-invocations").json()[0]["status"] == "running"
        with pytest.raises(ServiceError) as error:
            client.portal.call(call)
        assert error.value.code == "model_result_unavailable" and sends == [1]


def test_cancelling_stream_records_unknown_without_recovery_send(client):
    entry(client, channel(client))
    pat = token(client, ["model:invoke"])["token"]
    runtime = ModelRuntime(internal_service(client, pat))
    identifier = uuid4()
    data = schemas.Invoke(
        model_key="chat", messages=[{"role": "user", "content": "test"}], stream=True
    )

    async def abandon():
        stream = runtime.events(data, invocation_id=identifier)
        assert (await anext(stream))["type"] == "start"
        await stream.aclose()

    client.portal.call(abandon)
    row = client.get("/api/v1/model-invocations/" + str(identifier)).json()
    assert row["status"] == "unknown" and row["error_code"] == "model_interrupted"


def test_provider_balance_units_and_missing_stream_usage_are_not_invented(client, monkeypatch):
    deep = channel(client, "deepseek")
    ali = channel(client, "aliyun_bailian")
    a = client.post(f"/api/v1/model-channels/{deep['id']}/balance").json()
    b = client.post(f"/api/v1/model-channels/{ali['id']}/balance").json()
    assert (
        len(a["balance_snapshot"]["items"]) == 3
        and a["balance_snapshot"]["scope"] == "provider_account"
    )
    assert b["balance_snapshot"] == {
        "scope": "aliyun_account",
        "items": [
            {"kind": "available", "amount": "100.00", "unit": "CNY"},
            {"kind": "cash", "amount": "80.00", "unit": "CNY"},
        ],
    }
    entry(client, deep)

    async def respond(provider, request):
        data = {
            "id": "no-usage",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "mock-chat",
            "choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}],
        }
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=mock.MockStream(
                [("data: " + json.dumps(data) + "\n\n").encode(), b"data: [DONE]\n\n"]
            ),
        )

    install_transport(client, monkeypatch, respond)
    assert '"type": "done"' in invoke(client, stream=True).text
    row = client.get("/api/v1/model-invocations").json()[0]
    assert row["status"] == "succeeded" and row["usage"] is None and row["cost_amount"] is None


def test_http_rejection_is_failed_and_not_a_free_call_claim(client, monkeypatch):
    entry(client, channel(client))
    calls = []

    async def respond(provider, request):
        calls.append(1)
        return httpx2.Response(
            429,
            json={"error": {"message": "unsafe details " + SECRET}},
            headers={"retry-after": "0"},
        )

    install_transport(client, monkeypatch, respond)
    assert invoke(client).json()["code"] == "model_rate_limited" and calls == [1]
    rows = client.get("/api/v1/model-invocations").json()
    assert len(rows) == 1
    row = rows[0]
    assert (
        row["status"] == "failed" and row["cost_source"] == "unknown" and row["cost_amount"] is None
    )

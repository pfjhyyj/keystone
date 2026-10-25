"""Original-token billing over real SDK/SQL transactions; no public provider calls."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from uuid import UUID, uuid4

import httpx2
import pytest
from dbos import DBOS
from sqlalchemy import event, func, select
from support import model_stub
from test_files_dictionary import client as client
from test_files_dictionary import upload
from test_flow import start, wait_run, worker
from test_identity import query
from test_maintenance import executions, wait_for
from test_model_access import SECRET, channel, edit_channel, entry, invoke
from test_model_access import model_configuration as model_configuration

from keystone.core.errors import ServiceError
from keystone.core.time import utcnow
from keystone.platform.flow.adapters.dbos import Engine
from keystone.platform.flow.models import FlowRun
from keystone.platform.model_access.models import Channel, ModelInvocation
from keystone.platform.model_access.services.costs import CostService
from keystone.platform.model_access.tasks import workflows as jobs

pytestmark = pytest.mark.postgres


def test_newapi_and_original_site_channels_share_original_token_billing_scope(client, billing):
    old = channel(client, "openlux", api_base="https://api.openlux.ai/v1")
    new = channel(client, "newapi", api_base=old["api_base"])
    entry(client, old, key="original")
    entry(client, new, key="unified")
    ids = [invoke(client, key=key).json()["invocation_id"] for key in ("original", "unified")]
    for identifier in ids:
        response = client.post(f"/api/v1/model-invocations/{identifier}/cost")
        assert response.status_code == 200, response.text
        assert response.json()["cost_check_status"] == "confirmed"
        assert response.json()["cost_unit"] == "USD"
    owners = []
    for config in (old, new):
        identifier = UUID(config["id"])
        owners.append(query(client, lambda s, cid=identifier: s.get(Channel, cid)))
    assert owners[0].cost_context == owners[1].cost_context


def receipt(identifier, quota=1328, **overrides):
    return {
        "id": 1,
        "type": 2,
        "model_name": "mock-chat",
        "quota": quota,
        "other": json.dumps({"request_id": identifier}),
        "username": "must-not-persist@example.test",
        "content": "private-log-content",
        **overrides,
    }


@pytest.fixture
def billing(client, monkeypatch):
    from keystone import composition
    from keystone.examples.document_review.contracts import PACKAGE

    monkeypatch.setattr(composition, "FLOW_PACKAGES", (PACKAGE,))
    composition.execution_version.cache_clear()
    state = {
        "records": [],
        "calls": [],
        "ids": [],
        "error": None,
        "on_log": None,
        "inference_status": None,
        "header": True,
        "policy": {"quota_per_unit": 500000, "currency_type": "USD"},
    }

    async def respond(request):
        path = request.url.path
        state["calls"].append((request.method, path, dict(request.url.params)))
        if request.method == "POST":
            original = await model_stub.transport("openlux").handle_async_request(request)
            identifier = "bill-" + request.headers["x-keystone-invocation-id"]
            state["ids"].append(identifier)
            state["records"].append(receipt(identifier))
            if state["header"]:
                original.headers["x-api-request-id"] = identifier
            original.headers["x-oneapi-request-id"] = "wrapper-" + identifier
            original.headers["x-request-id"] = "trace-" + identifier
            if state["inference_status"]:
                original.status_code = state["inference_status"]
            return original
        if path == "/api/status":
            assert "key" not in request.url.params
            return httpx2.Response(200, json={"success": True, "data": state["policy"]})
        assert path == "/api/log/token"
        assert request.url.params["key"] == SECRET
        assert "request_id" not in request.url.params
        assert "authorization" not in request.headers
        if state["on_log"]:
            await state["on_log"]()
        if isinstance(state["error"], Exception):
            raise state["error"]
        if state["error"]:
            return httpx2.Response(state["error"], headers={"retry-after": "60"})
        page, size = int(request.url.params["p"]), int(request.url.params["page_size"])
        selected = [
            dict(row, id=index + page * 1000000)
            for index, row in enumerate(state["records"][(page - 1) * size : page * size])
        ]
        return httpx2.Response(
            200,
            json={
                "success": True,
                "data": {
                    "items": selected,
                    "total": len(state["records"]),
                    "page": page,
                    "page_size": size,
                },
            },
        )

    monkeypatch.setattr(
        client.app.state.runtime.models.http,
        "transport",
        lambda provider: httpx2.MockTransport(respond),
    )
    return state


def configure(client, provider="openlux", key="chat", **overrides):
    base = "https://api.openlux.ai/v1" if provider == "openlux" else "https://yunwu.ai/v1"
    config = channel(client, provider, api_base=base, **overrides)
    entry(client, config, key=key)
    return config


def call(client, key="chat", **kwargs):
    result = invoke(client, key=key, **kwargs)
    assert result.status_code == 200, result.text
    return result.json()["invocation_id"]


def view(client, identifier):
    return client.get(f"/api/v1/model-invocations/{identifier}").json()


def stored(client, identifier):
    return query(client, lambda session: session.get(ModelInvocation, UUID(identifier)))


def due(client, identifier, **changes):
    async def update(session):
        row = await session.get(ModelInvocation, UUID(identifier))
        row.cost_next_check_at = utcnow() - timedelta(seconds=1)
        for name, value in changes.items():
            setattr(row, name, value)

    query(client, update)


def process(client, identifier, service=None):
    runtime = client.app.state.runtime
    service = service or CostService(runtime.database, runtime.models)
    client.portal.call(service.reconcile, stored(client, identifier).cost_context["scope"])


def logs(state):
    return [request for request in state["calls"] if request[1] == "/api/log/token"]


def test_exact_header_receipt_and_idempotent_actual_cost(client, billing):
    configure(client)
    identifier = call(client)
    first = view(client, identifier)
    assert first["status"] == "succeeded" and first["cost_check_status"] == "pending"
    assert first["upstream_request_id"] == billing["ids"][0]
    original = stored(client, identifier)
    assert original.receipt_ids["body_id"] != original.upstream_request_id
    assert original.cost_context["unit"] == "USD"
    due(client, identifier)
    process(client, identifier)
    confirmed = view(client, identifier)
    assert confirmed["cost_amount"] == "0.002656000000000000"
    assert confirmed["cost_unit"] == "USD" and confirmed["cost_source"] == "query"
    assert confirmed["cost_check_status"] == "confirmed" and confirmed["cost_next_check_at"] is None
    assert confirmed["finished_at"] == first["finished_at"]
    evidence = stored(client, identifier).cost_evidence
    assert evidence["quota"] == 1328 and evidence["quota_per_unit"] == 500000
    assert "id" not in evidence and "private-log-content" not in json.dumps(evidence)
    assert "must-not-persist" not in json.dumps(evidence)
    for _ in range(2):
        assert (
            client.post(f"/api/v1/model-invocations/{identifier}/cost").json()["cost_amount"]
            == confirmed["cost_amount"]
        )
    assert len(client.get("/api/v1/model-invocations").json()) == 1
    assert (
        query(client, lambda session: session.scalar(select(func.count()).select_from(FlowRun)))
        == 0
    )
    assert len([item for item in billing["calls"] if item[0] == "POST"]) == 1
    assert client.get("/api/v1/model-invocations/summary").json()["cost_checks"] == {"confirmed": 1}


@pytest.mark.parametrize("conflict", [False, True])
def test_pagination_resume_does_not_confirm_until_full_window(client, billing, conflict):
    configure(client)
    identifier = call(client)
    target = billing["ids"][0]
    billing["records"] = (
        [receipt(target)]
        + [receipt(f"unrelated-{index}") for index in range(40)]
        + [receipt(target, 2000 if conflict else 1328)]
    )
    due(client, identifier)
    process(client, identifier)
    row = stored(client, identifier)
    assert row.cost_check_status == "pending" and row.cost_amount is None
    assert row.cost_check_rounds == 0
    owner = query(
        client,
        lambda session: session.scalar(
            select(Channel).where(Channel.cost_scope == row.cost_context["scope"])
        ),
    )
    assert owner.cost_checkpoint["page"] == 3
    first_window = {key: logs(billing)[0][2][key] for key in ("start_timestamp", "end_timestamp")}
    process(client, identifier)  # New service instance, persisted checkpoint only.
    row = stored(client, identifier)
    assert [item[2]["p"] for item in logs(billing)] == ["1", "2", "3"]
    assert all(
        all(item[2][key] == value for key, value in first_window.items()) for item in logs(billing)
    )
    assert row.cost_check_status == ("blocked" if conflict else "confirmed")
    assert (row.cost_amount is None) == conflict
    assert row.cost_check_rounds == 1


def test_wrong_id_waits_and_next_round_finds_delayed_log(client, billing):
    configure(client)
    identifier = call(client)
    target = billing["ids"][0]
    billing["records"] = [receipt("wrapper-" + target)]
    due(client, identifier)
    process(client, identifier)
    waiting = stored(client, identifier)
    assert waiting.cost_amount is None and waiting.cost_check_status == "pending"
    assert waiting.cost_check_reason == "cost_not_reported"
    assert waiting.cost_next_check_at > utcnow()
    billing["records"] = [receipt(target)]
    due(client, identifier)
    process(client, identifier)
    assert view(client, identifier)["cost_check_status"] == "confirmed"
    assert [item[2]["p"] for item in logs(billing)] == ["1", "1"]


def test_same_token_channels_share_one_query_and_cooldown(client, billing):
    first = configure(client)
    second = configure(client, key="second")
    a, b = call(client), call(client, "second")
    assert stored(client, a).cost_context == stored(client, b).cost_context
    due(client, a)
    due(client, b)
    billing["error"] = 429
    process(client, a)
    for identifier in (a, b):
        row = stored(client, identifier)
        assert row.cost_check_reason == "cost_rate_limited"
        assert row.cost_check_errors == 1
        assert row.cost_next_check_at >= utcnow() + timedelta(seconds=55)
    process(client, b)
    assert len(logs(billing)) == 1
    owner = query(client, lambda session: session.get(Channel, UUID(first["id"])))
    assert owner.cost_next_allowed_at
    assert edit_channel(client, second, api_key="a-different-token").status_code == 409
    assert edit_channel(client, second, api_base="https://yunwu.ai/v1").status_code == 409
    assert stored(client, a).cost_context["scope"] not in json.dumps(view(client, a))


def test_auth_block_and_explicit_manual_repair(client, billing):
    configure(client)
    identifier = call(client)
    due(client, identifier)
    billing["error"] = 401
    process(client, identifier)
    row = view(client, identifier)
    assert row["cost_check_status"] == "blocked" and row["status"] == "succeeded"
    assert row["cost_check_reason"] == "cost_auth_required" and row["error_code"] is None
    process(client, identifier)
    assert len(logs(billing)) == 1
    billing["error"] = None
    repaired = client.post(f"/api/v1/model-invocations/{identifier}/cost")
    assert repaired.status_code == 200 and repaired.json()["cost_check_status"] == "confirmed"
    audits = client.get("/api/v1/audit-logs?action=model.cost.recheck").json()
    assert len(audits) == 1 and audits[0]["actor_type"] == "user"


@pytest.mark.parametrize("reason", ["network", "quota"])
def test_safe_query_failure_classification(client, billing, reason):
    # The parsing matrix lives in unit/test_billing_validation.py; retain both persisted states.
    configure(client)
    identifier = call(client)
    due(client, identifier)
    if reason == "network":
        billing["error"] = httpx2.ConnectError("https://api.openlux.ai/api/log/token?key=" + SECRET)
    else:
        billing["records"][0]["quota"] = -1
    process(client, identifier)
    row = view(client, identifier)
    assert row["cost_check_status"] == ("pending" if reason == "network" else "blocked")
    assert row["cost_check_reason"] == (
        "cost_transport_failed" if reason == "network" else "cost_invalid_response"
    )
    assert row["cost_amount"] is None
    assert SECRET not in json.dumps(row)
    assert "key=" not in str(row["cost_check_reason"])


def test_retry_budget_and_expiry_never_mean_zero_cost(client, billing):
    config = configure(client)
    identifier = call(client)
    due(client, identifier, cost_check_errors=12)
    billing["error"] = 503
    process(client, identifier)
    assert view(client, identifier)["cost_check_reason"] == "cost_retries_exhausted"
    other = call(client)
    due(client, other, cost_deadline_at=utcnow() - timedelta(seconds=1))
    service = CostService(client.app.state.runtime.database, client.app.state.runtime.models)
    client.portal.call(service.scopes)
    assert view(client, other)["cost_check_status"] == "exhausted"
    assert len(logs(billing)) == 1
    assert view(client, identifier)["cost_amount"] is None
    assert edit_channel(client, config, name="still-manageable").status_code == 200


def test_query_then_crash_repeats_read_without_repeating_inference(client, billing, monkeypatch):
    configure(client)
    identifier = call(client)
    due(client, identifier)
    runtime = client.app.state.runtime
    service = CostService(runtime.database, runtime.models)

    async def crash(*args, **kwargs):
        raise RuntimeError("test crash after read")

    monkeypatch.setattr(service, "_commit", crash)
    with pytest.raises(RuntimeError, match="test crash after read"):
        process(client, identifier, service)
    assert view(client, identifier)["cost_amount"] is None
    process(client, identifier)
    assert view(client, identifier)["cost_check_status"] == "confirmed"
    assert len(logs(billing)) == 2
    assert len([item for item in billing["calls"] if item[0] == "POST"]) == 1


def test_configuration_change_discards_inflight_cost(client, billing):
    config = configure(client)
    identifier = call(client)
    due(client, identifier)
    runtime = client.app.state.runtime

    async def change():
        async with runtime.database.transaction() as session:
            row = await session.get(Channel, UUID(config["id"]))
            row.api_base = "https://changed.invalid/v1"

    billing["on_log"] = change
    with pytest.raises(ServiceError) as stale:
        process(client, identifier)
    assert stale.value.code == "model_configuration_changed"
    assert view(client, identifier)["cost_amount"] is None


def test_zero_and_legacy_yunwu_unit_are_explicit(client, billing):
    configure(client, "yunwu")
    billing["policy"] = {
        "quota_per_unit": 500000,
        "currency_type": "CUSTOM",
        "custom_currency_exchange_rate": 1,
    }
    identifier = call(client)
    billing["records"][0]["quota"] = 0
    due(client, identifier)
    process(client, identifier)
    row = view(client, identifier)
    assert row["cost_check_status"] == "confirmed"
    assert row["cost_unit"] == "yunwu_credits" and row["cost_amount"] == "0.000000000000000000"


@pytest.mark.parametrize("restart", [False, True])
def test_real_dbos_scheduler_queries_costs(client, billing, tmp_path, restart):
    configure(client)
    identifier = call(client)
    due(client, identifier)
    if restart:
        billing["records"] = [receipt(f"other-{index}") for index in range(40)] + billing["records"]
    runtime = client.app.state.runtime
    DBOS.migrate(runtime.settings.database_url.get_secret_value())
    runtime.flow_engine = Engine(runtime.settings)
    records = tmp_path / "billing-records.json"
    records.write_text(json.dumps(billing["records"]), encoding="utf8")
    code = f"""
import json, os
import httpx2
from keystone.platform.model_access.adapters.http import HttpClient
original = HttpClient.transport
async def respond(request):
    assert request.method == 'GET'
    if request.url.path == '/api/status':
        return httpx2.Response(200, json={{'success': True, 'data': {{
            'quota_per_unit': 500000, 'currency_type': 'USD'
        }}}})
    rows = json.loads(open({str(records)!r}, encoding='utf8').read())
    page, size = int(request.url.params['p']), int(request.url.params['page_size'])
    return httpx2.Response(200, json={{'success': True, 'data': {{
        'items': rows[(page-1)*size:page*size], 'total': len(rows),
        'page': page, 'page_size': size
    }}}})
HttpClient.transport = lambda self, provider: (
    httpx2.MockTransport(respond) if provider == 'openlux' else original(self, provider)
)
from keystone.entrypoints.worker import main
main()
"""
    if restart:

        def checkpoint_saved():
            owner = query(client, lambda session: session.scalar(select(Channel)))
            return owner.cost_checkpoint and owner.cost_checkpoint["page"] == 3

        with worker(client, tmp_path, code=code) as (process, log):
            wait_for(client, process, log, checkpoint_saved)
        assert view(client, identifier)["cost_amount"] is None
    with worker(client, tmp_path, code=code) as (process, log):
        # The cost transaction commits before DBOS records workflow completion.
        wait_for(
            client,
            process,
            log,
            lambda: (
                view(client, identifier)["cost_check_status"] == "confirmed"
                and executions(client, jobs.QUERY_WORKFLOW, status="SUCCESS")
            ),
        )
        assert executions(client, jobs.QUERY_WORKFLOW, status="SUCCESS")
        wait_for(
            client,
            process,
            log,
            lambda: client.get("/api/v1/worker/status").json()["status"] == "ready",
        )
        # New work after startup must be picked up by the persistent schedule.
        other = call(client)
        next_records = tmp_path / "billing-next.json"
        next_records.write_text(json.dumps(billing["records"]), encoding="utf8")
        next_records.replace(records)
        due(client, other)
        wait_for(
            client,
            process,
            log,
            lambda: (
                view(client, other)["cost_check_status"] == "confirmed"
                and len(executions(client, jobs.QUERY_WORKFLOW, status="SUCCESS")) >= 2
            ),
        )
        assert len(executions(client, jobs.QUERY_WORKFLOW, status="SUCCESS")) >= 2
        assert SECRET not in log.read_text(encoding="utf8")
    assert view(client, identifier)["cost_amount"] == "0.002656000000000000"
    client.portal.call(runtime.flow_engine.close)
    runtime.flow_engine = None


@pytest.mark.parametrize("header", [False, True])
def test_failed_call_can_be_billed_without_changing_result(client, billing, header):
    configure(client)
    billing["inference_status"], billing["header"] = 400, header
    response = invoke(client)
    assert response.status_code != 200
    identifier = client.get("/api/v1/model-invocations").json()[0]["id"]
    before = view(client, identifier)
    assert before["status"] == "failed"
    if header:
        due(client, identifier)
        process(client, identifier)
        after = view(client, identifier)
        assert after["cost_check_status"] == "confirmed"
        assert after["cost_amount"] == "0.002656000000000000"
        assert (after["status"], after["error_code"], after["finished_at"]) == (
            before["status"],
            before["error_code"],
            before["finished_at"],
        )
    else:
        assert before["upstream_request_id"] is None
        assert before["cost_check_status"] == "unavailable"
        assert before["cost_check_reason"] == "cost_request_id_missing"
        assert not logs(billing)
    assert len([item for item in billing["calls"] if item[0] == "POST"]) == 1


def test_same_receipt_cannot_charge_two_local_calls(client, billing):
    configure(client)
    first, second = call(client), call(client)
    duplicate = billing["ids"][0]
    due(client, first)
    due(client, second, upstream_request_id=duplicate)
    process(client, first)
    for identifier in (first, second):
        row = view(client, identifier)
        assert row["cost_check_status"] == "blocked"
        assert row["cost_check_reason"] == "cost_receipt_conflict"
        assert row["cost_amount"] is None


def test_manual_recheck_prioritizes_requested_row_beyond_batch(client, billing):
    configure(client)
    identifiers = [call(client) for _ in range(22)]
    for identifier in identifiers:
        due(client, identifier)
    target = identifiers[-1]
    # The manual endpoint must actually inspect this invocation, even behind a full batch.
    response = client.post(f"/api/v1/model-invocations/{target}/cost")
    assert response.status_code == 200
    assert response.json()["cost_check_status"] == "confirmed"
    assert view(client, identifiers[0])["cost_check_status"] == "pending"


def test_manual_and_background_share_scope_lock(client, billing):
    configure(client)
    identifier = call(client)
    due(client, identifier)
    started, release = Event(), Event()

    async def hold():
        started.set()
        await asyncio.to_thread(release.wait, 10)

    billing["on_log"] = hold
    with ThreadPoolExecutor(max_workers=1) as executor:
        background = executor.submit(process, client, identifier)
        try:
            assert started.wait(10)
            response = client.post(f"/api/v1/model-invocations/{identifier}/cost")
            assert response.status_code == 409
            assert response.json()["code"] == "model_refresh_busy"
        finally:
            release.set()
        background.result(timeout=15)
    assert view(client, identifier)["cost_check_status"] == "confirmed"
    assert len(logs(billing)) == 1


def test_cost_and_checkpoint_rollback_together(client, billing):
    configure(client)
    identifier = call(client)
    due(client, identifier)
    engine = client.app.state.runtime.database.engine.sync_engine
    failed = False

    def fail_after_update(connection, cursor, statement, parameters, context, many):
        nonlocal failed
        if not failed and statement.startswith("UPDATE keystone.model_invocations"):
            failed = True
            raise RuntimeError("test transaction failure after fee write")

    async def fail_commit():
        event.listen(engine, "after_cursor_execute", fail_after_update)

    billing["on_log"] = fail_commit
    try:
        with pytest.raises(RuntimeError, match="test transaction failure"):
            process(client, identifier)
    finally:
        event.remove(engine, "after_cursor_execute", fail_after_update)
        billing["on_log"] = None
    assert failed and view(client, identifier)["cost_amount"] is None
    owner = query(client, lambda session: session.scalar(select(Channel)))
    assert owner.cost_checkpoint["page"] == 1
    process(client, identifier)
    assert view(client, identifier)["cost_check_status"] == "confirmed"
    assert len(logs(billing)) == 2


@pytest.mark.parametrize("cancel", [False, True])
def test_flow_completion_or_cancel_and_channel_disable_preserve_billing(
    client, billing, tmp_path, cancel
):
    from support.flow_fixture.contracts import definition

    config = configure(client)
    runtime = client.app.state.runtime
    runtime.flow_definitions = {definition.key: definition}
    DBOS.migrate(runtime.settings.database_url.get_secret_value())
    runtime.flow_engine = Engine(runtime.settings)
    run = start(client, upload(client, b"Review this document.").json()["id"]).json()
    records = tmp_path / "flow-billing.json"
    records.write_text("[]", encoding="utf8")
    code = f"""
import asyncio, json
from pathlib import Path
import httpx2
from support import model_stub
from keystone.platform.model_access.adapters.http import HttpClient
original = HttpClient.transport
records = Path({str(records)!r})
class DelayedResponse(httpx2.AsyncByteStream):
    def __init__(self, content):
        self.content = content
    async def __aiter__(self):
        await asyncio.sleep(4)
        yield self.content
async def respond(request):
    rows = json.loads(records.read_text(encoding='utf8'))
    if request.method == 'POST':
        identifier = 'bill-' + request.headers['x-keystone-invocation-id']
        rows.append({{'type': 2, 'quota': 1328, 'other': {{'request_id': identifier}},
                     'model_name': json.loads(request.content)['model']}})
        records.write_text(json.dumps(rows), encoding='utf8')
        response = await model_stub.transport('openlux').handle_async_request(request)
        response.headers['x-api-request-id'] = identifier
        if {cancel!r}:
            return httpx2.Response(200, headers=response.headers,
                                  stream=DelayedResponse(response.content))
        return response
    if request.url.path == '/api/status':
        return httpx2.Response(200, json={{'success': True, 'data': {{
            'quota_per_unit': 500000, 'currency_type': 'USD'
        }}}})
    return httpx2.Response(200, json={{'success': True, 'data': {{
        'items': rows, 'total': len(rows), 'page': 1, 'page_size': 20
    }}}})
HttpClient.transport = lambda self, provider: (
    httpx2.MockTransport(respond) if provider == 'openlux' else original(self, provider)
)
from keystone.entrypoints.worker import main
main()
"""

    def invocations():
        return client.get(f"/api/v1/model-invocations?flow_run_id={run['id']}").json()

    with worker(client, tmp_path, code=code) as (process, log):
        if cancel:
            wait_for(
                client,
                process,
                log,
                lambda: any(row["upstream_request_id"] for row in invocations()),
            )
            assert client.post(f"/api/v1/flow-runs/{run['id']}/cancel").status_code == 200
        ended = wait_run(client, run["id"], process, log)
        assert ended["status"] == ("cancelled" if cancel else "succeeded")
        # Cancellation does not revoke an already sent provider request.
        wait_for(
            client, process, log, lambda: all(row["status"] != "running" for row in invocations())
        )
        before = invocations()
        assert len(before) == (1 if cancel else 2)
        assert edit_channel(client, config, is_active=False).status_code == 200
        for row in before:
            due(client, row["id"])
        wait_for(
            client,
            process,
            log,
            lambda: all(row["cost_check_status"] == "confirmed" for row in invocations()),
        )
        after = invocations()
        assert [(row["id"], row["status"], row["error_code"]) for row in after] == [
            (row["id"], row["status"], row["error_code"]) for row in before
        ]
        assert all(row["cost_amount"] == "0.002656000000000000" for row in after)
        assert len(json.loads(records.read_text(encoding="utf8"))) == len(before)
        assert client.get(f"/api/v1/flow-runs/{run['id']}").json()["result"] == ended["result"]
    client.portal.call(runtime.flow_engine.close)
    runtime.flow_engine = None


def test_unreadable_original_credential_blocks_without_http(client, billing):
    configure(client)
    identifier = call(client)
    due(client, identifier)

    async def corrupt(session):
        owner = await session.scalar(select(Channel))
        owner.api_key_encrypted = "unreadable-ciphertext"

    query(client, corrupt)
    process(client, identifier)
    row = view(client, identifier)
    assert row["cost_check_status"] == "blocked"
    assert row["cost_check_reason"] == "cost_configuration_unavailable"
    assert row["cost_amount"] is None and not logs(billing)


def test_concurrency_budget_covers_database_coordination(client, monkeypatch):
    runtime = client.app.state.runtime
    service = CostService(runtime.database, runtime.models)
    active, peak = 0, 0

    async def prepare(*args):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.1)
        finally:
            active -= 1

    monkeypatch.setattr(service, "_prepare", prepare)

    async def concurrent():
        await asyncio.gather(*(service.reconcile(uuid4().hex * 2) for _ in range(6)))

    client.portal.call(concurrent)
    assert peak == 2


@pytest.mark.parametrize("missing", [False, True])
def test_openrouter_missing_response_cost_uses_direct_lookup(client, monkeypatch, missing):
    entry(client, channel(client))
    sent = []

    async def respond(request):
        sent.append(request.method)
        if request.method == "POST":
            original = await model_stub.transport("openrouter").handle_async_request(request)
            payload = original.json()
            payload["usage"].pop("cost")
            return httpx2.Response(200, json=payload)
        assert request.url.path == "/api/v1/generation"
        assert request.headers["authorization"] == "Bearer " + SECRET
        assert "key" not in request.url.params
        if missing:
            return httpx2.Response(404, json={"error": {"code": 404}})
        return httpx2.Response(
            200, json={"data": {"id": request.url.params["id"], "total_cost": "0.0031"}}
        )

    monkeypatch.setattr(
        client.app.state.runtime.models.http,
        "transport",
        lambda provider: httpx2.MockTransport(respond),
    )
    identifier = call(client)
    assert view(client, identifier)["cost_check_status"] == "pending"
    due(client, identifier)
    process(client, identifier)
    row = view(client, identifier)
    assert row["cost_check_status"] == ("pending" if missing else "confirmed")
    assert row["cost_amount"] == (None if missing else "0.003100000000000000")
    assert row["cost_unit"] == (None if missing else "credits")
    assert sent == ["POST", "GET"]


def test_another_billing_contract_reuses_inference_and_reconciliation(client, billing, monkeypatch):
    from dataclasses import replace

    from keystone.platform.model_access.adapters.billing.contracts import Binding, LogPage, Receipt
    from keystone.platform.model_access.adapters.channels import CHANNELS

    pages = []

    class OtherBilling:
        cost_query_mode = "logs"
        uses_billing_key = False

        def binding(self, api_base):
            return Binding("https://api.openlux.ai", "logs", "test_credits", 100, "test-v1")

        def receipt(self, identifiers, reported_cost):
            return Receipt(identifiers.get("x-api-request-id"))

        async def validate_policy(self, http, context, secret):
            assert context["policy"] == "test-v1" and context["unit"] == "test_credits"

        async def page(self, http, context, secret, **window):
            pages.append(window["page"])
            # A different pagination contract supplies its own continuation position.
            record = {"request_id": billing["ids"][0], "amount": "0.25", "unit": "test_credits"}
            return LogPage([record], 7 if len(pages) == 1 else 8, len(pages) == 2)

    monkeypatch.setitem(CHANNELS, "openlux", replace(CHANNELS["openlux"], billing=OtherBilling()))
    configure(client)
    identifier = call(client)
    due(client, identifier)
    process(client, identifier)
    row = view(client, identifier)
    assert row["cost_check_status"] == "confirmed"
    assert row["cost_amount"] == "0.250000000000000000"
    assert row["cost_unit"] == "test_credits" and pages == [1, 7]
    assert not logs(billing)
    assert len([item for item in billing["calls"] if item[0] == "POST"]) == 1

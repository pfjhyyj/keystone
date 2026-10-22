"""Live mode over real loopback HTTP sockets, never the public OpenRouter service.

Only the final socket destination is replaced in tests. Product target validation,
SDK, retries, tracking, response parsing and PostgreSQL transactions remain real.
"""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx2
import pytest
from dbos import DBOS
from test_files_dictionary import client as client
from test_files_dictionary import upload
from test_flow import start, wait_run, worker
from test_identity import token
from test_model_access import BILLING, SECRET, channel, entry, internal_service, invoke

from keystone.core.config import Settings
from keystone.core.errors import ServiceError
from keystone.platform.flow.engine import Engine
from keystone.platform.model_access import gateway, schemas
from keystone.platform.model_access.services.runtime import ModelRuntime

NETWORK_TRANSPORT = httpx2.AsyncHTTPTransport


@pytest.mark.postgres
def test_live_catalog_and_sender_share_capability_declaration(client, live, monkeypatch):
    calls, _, _ = live
    configure(client)
    assert client.get("/api/v1/models").json()["items"]
    monkeypatch.setitem(
        gateway.SUPPORTED_TARGETS, "openrouter", (gateway.OPENROUTER_BASE, {"/credits": "GET"})
    )
    assert client.get("/api/v1/models").json()["items"] == []
    response = invoke(client)
    assert response.status_code == 409 and response.json()["code"] == "model_target_not_allowed"
    assert calls == []


def upstream_reply(method, path, headers, body):
    """Independent wire fixtures for the documented OpenRouter response shapes."""
    url = urlsplit(path)
    if method == "GET" and url.path == "/api/v1/credits":
        payload = {"data": {"total_credits": 100.0, "total_usage": 12.5}}
    elif method == "GET" and url.path == "/api/v1/generation":
        payload = {"data": {"id": parse_qs(url.query)["id"][0], "total_cost": 0.0012}}
    else:
        assert method == "POST" and url.path == "/api/v1/chat/completions"
        request = json.loads(body)
        envelope = {
            "id": "gen-" + headers["X-Keystone-Invocation-Id"],
            "created": 1770000000,
            "model": request["model"],
        }
        usage = {
            "prompt_tokens": 12,
            "completion_tokens": 8,
            "total_tokens": 20,
            "cost": 0.0012,
            # Upstream inference cost is not the account charge.
            "cost_details": {"upstream_inference_cost": 0.9},
        }
        message = {"role": "assistant", "content": "Local OpenRouter protocol fixture."}
        finish = "stop"
        if request.get("tools"):
            inspected = any(m["role"] == "tool" for m in request["messages"])
            name = "submit_review" if inspected else "inspect_document"
            args = {"verdict": "reviewed", "summary": "Local fixture review."} if inspected else {}
            if inspected:
                source = json.loads(
                    next(
                        m["content"]
                        for m in request["messages"]
                        if m.get("role") == "tool"
                        and m.get("tool_call_id") == "call-inspect_document"
                    )
                )
                args.update(
                    source_file_id=source["source_file_id"], characters=source["characters"]
                )
            message.update(
                content=None,
                tool_calls=[
                    {
                        "id": "call-" + name,
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)},
                    }
                ],
            )
            finish = "tool_calls"
        if request.get("stream"):
            events = [
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": message["content"]},
                            "finish_reason": None,
                        }
                    ]
                },
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                {"choices": [], "usage": usage},
            ]
            payload = (
                ": OPENROUTER PROCESSING\n\n"
                + "".join(
                    "data: "
                    + json.dumps({**envelope, "object": "chat.completion.chunk", **event})
                    + "\n\n"
                    for event in events
                )
                + "data: [DONE]\n\n"
            )
            return 200, "text/event-stream", payload.encode(), {}
        payload = {
            **envelope,
            "object": "chat.completion",
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": usage,
        }
    return 200, "application/json", json.dumps(payload).encode(), {}


def local_transport(port):
    """Test-only bridge, also installed in the independent DBOS Worker process."""

    class LoopbackTransport(NETWORK_TRANSPORT):
        def __init__(self, **kwargs):
            assert kwargs == {"retries": 0, "trust_env": False}
            super().__init__(**kwargs)

        async def handle_async_request(self, request):
            assert request.url.scheme == "https" and request.url.host == "openrouter.ai"
            assert request.url.port in {None, 443}
            request.url = request.url.copy_with(scheme="http", host="127.0.0.1", port=port)
            return await super().handle_async_request(request)

    return LoopbackTransport


@pytest.fixture
def live(client, monkeypatch):
    calls, overrides, server_errors = [], {}, []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self):
            try:
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                calls.append((self.command, self.path, dict(self.headers), body))
                path = self.path.split("?")[0]
                if path in overrides:
                    status, content_type, payload, headers = overrides[path]
                else:
                    status, content_type, payload, headers = upstream_reply(
                        self.command, self.path, self.headers, body
                    )
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as error:
                server_errors.append(type(error).__name__)
                self.close_connection = True

        do_GET = do_POST = respond

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(httpx2, "AsyncHTTPTransport", local_transport(server.server_port))
    # Even an inherited environment proxy must not be used by the live transport.
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    try:
        yield calls, overrides, server.server_port
        assert not server_errors, server_errors
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def configure(client, **changes):
    row = channel(client, api_base=gateway.OPENROUTER_BASE, **changes)
    entry(client, row)
    return row


def test_real_transport_is_the_default_without_mode_configuration():
    settings = Settings(config_file=None)
    assert "model_mode" not in Settings.model_fields
    transport = gateway.Gateway(settings).transport("openrouter")
    assert isinstance(transport, httpx2.AsyncHTTPTransport)
    with pytest.raises(ServiceError, match="OpenRouter"):
        gateway.Gateway(settings).transport("deepseek")


@pytest.mark.postgres
def test_live_sdk_stream_cost_query_balance_and_credential_separation(client, live):
    calls, _, _ = live
    channel_row = configure(client)
    result = invoke(client)
    assert result.status_code == 200 and "mode" not in result.json(), result.text
    events = [
        json.loads(line[6:])
        for line in invoke(client, stream=True).text.splitlines()
        if line.startswith("data: ")
    ]
    assert [e["type"] for e in events] == ["start", "delta", "done"]
    assert "mode" not in events[0] and "mode" not in events[-1]
    identifier = result.json()["invocation_id"]
    detail = client.get(f"/api/v1/model-invocations/{identifier}").json()
    assert detail["cost_amount"] == "0.001200000000000000" and detail["cost_source"] == "response"
    assert detail["usage"]["total_tokens"] == 20
    queried = client.post(f"/api/v1/model-invocations/{identifier}/cost").json()
    assert queried["cost_amount"] == detail["cost_amount"] and queried["cost_source"] == "query"
    balance = client.post(f"/api/v1/model-channels/{channel_row['id']}/balance").json()
    assert balance["balance_check_status"] == "ok"
    assert balance["balance_snapshot"]["items"] == [
        {"kind": "remaining", "amount": "87.5", "unit": "credits"}
    ]
    assert len(calls) == 4
    for method, path, headers, _ in calls:
        expected = BILLING if path.endswith("/credits") else SECRET
        assert headers["Authorization"] == "Bearer " + expected
        assert method == ("POST" if path.endswith("/chat/completions") else "GET")


@pytest.mark.postgres
def test_live_rejects_untrusted_targets_and_unsupported_providers_before_send(client, live):
    calls, _, _ = live
    bad = [
        "https://mock.invalid/v1",
        "https://127.0.0.1/api/v1",
        "https://openrouter.ai.evil.invalid/api/v1",
        "https://openrouter.ai/api",
        "https://openrouter.ai:444/api/v1",
        "https://openrouter.ai/api/v1/other",
    ]
    for index, base in enumerate(bad):
        entry(client, channel(client, api_base=base), key=f"bad{index}")
        response = invoke(client, key=f"bad{index}")
        assert response.status_code == 409 and response.json()["code"] == "model_target_not_allowed"
    for provider in ("deepseek", "aliyun_bailian"):
        entry(client, channel(client, provider, api_base=gateway.OPENROUTER_BASE), key=provider)
        response = invoke(client, key=provider)
        assert (
            response.status_code == 503 and response.json()["code"] == "model_provider_unavailable"
        )
    assert client.get("/api/v1/models").json() == {"items": []}
    assert client.get("/api/v1/model-invocations").json() == []

    async def invalid_query():
        for base, path in [
            (bad[0], "/credits"),
            (gateway.OPENROUTER_BASE, "/keys"),
            (gateway.OPENROUTER_BASE, "/chat/completions"),
        ]:
            with pytest.raises(ServiceError):
                await client.app.state.runtime.models.query("openrouter", base, path, BILLING)

    client.portal.call(invalid_query)
    assert calls == []


@pytest.mark.postgres
def test_live_no_retry_redirect_or_raw_error_disclosure(client, live, caplog):
    calls, overrides, _ = live
    configure(client)
    for status, code, state in [
        (402, "model_insufficient_credits", "failed"),
        (429, "model_rate_limited", "failed"),
        (500, "model_upstream_error", "unknown"),
        (307, "model_upstream_error", "unknown"),
    ]:
        overrides["/api/v1/chat/completions"] = (
            status,
            "application/json",
            json.dumps({"error": {"message": SECRET}}).encode(),
            {"Retry-After": "0", "Location": "https://untrusted.invalid/steal"},
        )
        before = len(calls)
        response = invoke(client)
        assert response.status_code == 502 and response.json()["code"] == code, response.text
        assert SECRET not in response.text and len(calls) == before + 1
        row = client.get("/api/v1/model-invocations").json()[0]
        assert row["status"] == state and row["cost_amount"] is None
    assert SECRET not in caplog.text


@pytest.mark.postgres
@pytest.mark.parametrize("stream", [False, True])
def test_live_http_200_embedded_error_is_unknown_and_preserves_cost(client, live, stream):
    calls, overrides, _ = live
    configure(client)
    payload = json.dumps(
        {
            "id": "gen-error",
            "error": {"code": 502, "message": SECRET},
            "usage": {"cost": "0.0003"},
            "choices": [{"index": 0, "finish_reason": "error", "delta": {}}],
        }
    ).encode()
    if stream:
        payload = b": OPENROUTER PROCESSING\n\ndata: " + payload + b"\n\ndata: [DONE]\n\n"
    overrides["/api/v1/chat/completions"] = (
        200,
        "text/event-stream" if stream else "application/json",
        payload,
        {},
    )
    response = invoke(client, stream=stream)
    assert "model_upstream_error" in response.text and SECRET not in response.text
    assert len(calls) == 1
    row = client.get("/api/v1/model-invocations").json()[0]
    assert row["status"] == "unknown" and row["upstream_request_id"] == "gen-error"
    assert row["cost_amount"] == "0.000300000000000000" and row["cost_source"] == "response"


@pytest.mark.postgres
def test_live_query_failures_preserve_receipts_and_missing_billing_key_allows_inference(
    client, live
):
    calls, overrides, _ = live
    row = configure(client)
    identifier = invoke(client).json()["invocation_id"]
    path = f"/api/v1/model-channels/{row['id']}/balance"
    previous = client.post(path).json()["balance_snapshot"]
    overrides["/api/v1/credits"] = (
        403,
        "application/json",
        b'{"error":{"message":"Management key required"}}',
        {},
    )
    failed = client.post(path).json()
    assert failed["balance_check_status"] == "error" and failed["balance_snapshot"] == previous
    overrides["/api/v1/generation"] = (
        200,
        "application/json",
        b'{"data":{"id":"wrong-id","total_cost":42}}',
        {},
    )
    result = client.post(f"/api/v1/model-invocations/{identifier}/cost").json()
    assert result["cost_amount"] == "0.001200000000000000" and result["cost_source"] == "response"
    row = client.get("/api/v1/model-channels").json()[0]
    from test_model_access import edit_channel

    assert edit_channel(client, row, clear_billing_api_key=True).status_code == 200
    before = len(calls)
    assert client.post(path).json()["balance_check_status"] == "unavailable"
    assert len(calls) == before
    assert invoke(client).status_code == 200


@pytest.mark.postgres
def test_live_stable_attempt_id_never_sends_twice(client, live):
    calls, _, _ = live
    configure(client)
    service = internal_service(client, token(client, ["model:invoke"])["token"])

    async def repeat():
        runtime = ModelRuntime(service)
        data = schemas.Invoke(model_key="chat", messages=[{"role": "user", "content": "test"}])
        identifier = uuid4()
        await runtime.invoke(data, invocation_id=identifier)
        with pytest.raises(ServiceError) as error:
            await runtime.invoke(data, invocation_id=identifier)
        assert error.value.code == "model_result_unavailable"

    client.portal.call(repeat)
    assert len(calls) == 1


@pytest.mark.postgres
@pytest.mark.parametrize("upstream_status", [None, 400, 402, 404, 429])
def test_live_mode_real_dbos_worker_uses_same_send_boundary(
    client, live, tmp_path, upstream_status
):
    calls, overrides, port = live
    configure(client)
    if upstream_status:
        overrides["/api/v1/chat/completions"] = (
            upstream_status,
            "application/json",
            json.dumps({"error": {"message": SECRET}}).encode(),
            {},
        )
    runtime = client.app.state.runtime
    from flow_fixture.contracts import definition

    runtime.flow_definitions = {definition.key: definition}
    runtime.settings.flow_enabled = True
    DBOS.migrate(runtime.settings.database_url.get_secret_value(), schema="dbos")
    runtime.flow_engine = Engine(runtime.settings)
    file_id = upload(client, b"Local socket fixture for live mode.").json()["id"]
    run = start(client, file_id).json()
    # No production option allows bypassing the official target: the child imports this test bridge.
    code = (
        f"import sys, os; sys.path.insert(0, {str(Path(__file__).parent)!r}); "
        "import httpx2; from test_model_live import local_transport; "
        "from flow_fixture import install; install(); "
        f"httpx2.AsyncHTTPTransport = local_transport({port}); "
        "from keystone.entrypoints.worker import main; main()"
    )
    with worker(client, tmp_path, code=code, stub=False) as (process, log):
        done = wait_run(client, run["id"], process, log)
        assert done["status"] == ("failed" if upstream_status else "succeeded"), (
            done,
            log.read_text(encoding="utf-8"),
        )
    rows = client.get(f"/api/v1/model-invocations?flow_run_id={run['id']}").json()
    if upstream_status:
        code = {
            400: "model_invalid_request",
            402: "model_insufficient_credits",
            404: "upstream_model_not_found",
            429: "model_rate_limited",
        }[upstream_status]
        assert done["error_code"] == code
        assert len(rows) == len(calls) == 1
        assert rows[0]["status"] == "failed" and rows[0]["error_code"] == code
        assert SECRET not in json.dumps(done) and SECRET not in json.dumps(rows)
        assert rows[0]["cost_amount"] is None  # A rejection is not a free-call receipt.
        return
    assert len(rows) == len(calls) == 2
    assert all(r["status"] == "succeeded" and r["cost_source"] == "response" for r in rows)
    assert (
        client.get(f"/api/v1/files/{done['result']['report_file_id']}/content").status_code == 200
    )

"""Actual PostgreSQL scope spacing, rollback and cross-process cooldown; no paid requests."""

import asyncio
import time
from concurrent.futures import CancelledError
from contextlib import asynccontextmanager
from itertools import pairwise
from threading import Event

import httpx2
import pytest
from sqlalchemy import func, select, update
from support.flow_packages import install_package
from test_files_dictionary import client as client
from test_identity import query, token
from test_model_access import SECRET, channel, entry, internal_service, invoke
from test_model_access import model_configuration as model_configuration

from keystone.core.errors import ServiceError
from keystone.platform.model_access.models import ModelDispatchScope, ModelInvocation
from keystone.platform.model_access.schemas import Invoke
from keystone.platform.model_access.services.dispatch import ModelDispatchService
from keystone.platform.model_access.services.runtime import ModelRuntime

pytestmark = pytest.mark.postgres


def dispatch(client):
    runtime = client.app.state.runtime
    return ModelDispatchService(runtime.database, runtime.settings)


def credential_scope(client, secret=SECRET):
    return client.app.state.runtime.models.credentials.scope("https://openrouter.ai/api/v1", secret)


def test_duplicate_channels_share_spacing_and_another_token_remains_independent(
    client, monkeypatch
):
    runtime = client.app.state.runtime
    runtime.settings.model_send_interval_ms = 150
    entry(client, channel(client))
    entry(client, channel(client, name="duplicate"), key="duplicate")
    entry(client, channel(client, api_key="separate-token", name="independent"), key="other")
    calls = []
    original = runtime.models.http.transport

    def transport(provider):
        source = original(provider)

        async def send(request):
            calls.append(time.monotonic())
            return await source.handle_async_request(request)

        return httpx2.MockTransport(send)

    monkeypatch.setattr(runtime.models.http, "transport", transport)
    model = ModelRuntime(internal_service(client, token(client, ["model:invoke"])["token"]))

    async def exercise():
        return await asyncio.gather(
            *(
                model.invoke(Invoke(model_key=key, messages=[{"role": "user", "content": "x"}]))
                for key in ("chat", "duplicate", "chat", "duplicate")
            )
        )

    assert len(client.portal.call(exercise)) == 4
    assert len(calls) == 4
    assert all(b - a >= 0.12 for a, b in pairwise(sorted(calls)))
    assert (
        len(query(client, lambda session: session.scalars(select(ModelDispatchScope))).all()) == 1
    )
    client.portal.call(dispatch(client).cooldown, credential_scope(client), 120)
    assert invoke(client, key="duplicate").json()["code"] == "model_dispatch_delayed"
    assert len(calls) == 4
    assert invoke(client, key="other").status_code == 200
    assert len(calls) == 5
    assert len(query(client, lambda session: session.scalars(select(ModelInvocation))).all()) == 5


def test_cooling_token_releases_single_global_slot_until_eligible(client, monkeypatch):
    runtime = client.app.state.runtime
    runtime.settings.model_scope_concurrency = 1
    runtime.settings.model_inference_concurrency = 1
    cooling_entry = entry(client, channel(client))
    other_entry = entry(
        client, channel(client, api_key="separate-token", name="other"), key="other"
    )
    scope = credential_scope(client)
    client.portal.call(dispatch(client).cooldown, scope, 30)
    released = Event()
    resume = client.portal.call(asyncio.Event)
    original = runtime.models.coordination.inference

    @asynccontextmanager
    async def inference(key):
        async with original(key) as permit:
            yield permit
        if key == scope and not released.is_set():
            released.set()
            await resume.wait()

    monkeypatch.setattr(runtime.models.coordination, "inference", inference)
    model = ModelRuntime(internal_service(client, token(client, ["model:invoke"])["token"]))

    async def cooled_call():
        return await model.invoke(
            Invoke(model_key="chat", messages=[{"role": "user", "content": "x"}])
        )

    pending = client.portal.start_task_soon(cooled_call)
    try:
        assert released.wait(5)
        assert invoke(client, key="other").status_code == 200
        rows = query(client, lambda session: session.scalars(select(ModelInvocation))).all()
        assert [(str(row.model_entry_id), row.status) for row in rows] == [
            (other_entry["id"], "succeeded")
        ]
        query(
            client,
            lambda session: session.execute(
                update(ModelDispatchScope)
                .where(ModelDispatchScope.scope == scope)
                .values(blocked_until=func.clock_timestamp())
            ),
        )
    finally:
        client.portal.call(resume.set)
        if not released.is_set():
            pending.cancel()

    pending.result(timeout=5)
    rows = query(client, lambda session: session.scalars(select(ModelInvocation))).all()
    assert sorted((str(row.model_entry_id), row.status) for row in rows) == sorted(
        [(cooling_entry["id"], "succeeded"), (other_entry["id"], "succeeded")]
    )


def test_429_without_receipt_blocks_api_and_a_new_real_worker(client, monkeypatch, tmp_path):
    from support.operation_flow import PACKAGE, definition
    from test_flow import wait_run, worker

    runtime = client.app.state.runtime
    entry(client, channel(client), operations=["text.generate", "text.structured"])
    requests = []

    async def limited(request):
        requests.append(request)
        return httpx2.Response(
            429, headers={"retry-after": "120"}, json={"error": "private-upstream"}
        )

    monkeypatch.setattr(
        runtime.models.http, "transport", lambda provider: httpx2.MockTransport(limited)
    )
    assert invoke(client).json()["code"] == "model_rate_limited"
    scope = credential_scope(client)
    original = query(client, lambda session: session.get(ModelDispatchScope, scope))
    assert original.blocked_until > original.updated_at
    assert invoke(client).json()["code"] == "model_dispatch_delayed"
    assert len(requests) == 1

    install_package(client, monkeypatch, PACKAGE)
    response = client.post(
        "/api/v1/_test/review",
        json={
            "definition_key": definition.key,
            "request_key": "cooled-worker",
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
    assert done["status"] == "failed" and done["error_code"] == "model_dispatch_delayed", done
    rows = query(client, lambda session: session.scalars(select(ModelInvocation))).all()
    assert len(rows) == 1 and rows[0].status == "failed" and rows[0].receipt_ids is None
    after = query(client, lambda session: session.get(ModelDispatchScope, scope))
    assert after.blocked_until == original.blocked_until
    assert "private-upstream" not in str(done) and len(requests) == 1


def test_registration_failure_rolls_back_the_send_claim_and_never_opens_transport(
    client, monkeypatch
):
    runtime = client.app.state.runtime
    entry(client, channel(client))
    original = ModelDispatchService.claim_in_transaction

    async def fail(self, session, scope):
        await original(self, session, scope)
        raise RuntimeError("private-database-failure")

    monkeypatch.setattr(ModelDispatchService, "claim_in_transaction", fail)

    def forbidden(provider):
        async def send(request):
            raise AssertionError("No physical send after failed registration")

        return httpx2.MockTransport(send)

    monkeypatch.setattr(runtime.models.http, "transport", forbidden)
    response = invoke(client)
    assert response.status_code == 502
    assert "private-database-failure" not in response.text
    assert query(client, lambda session: session.scalar(select(ModelInvocation.id))) is None
    assert query(client, lambda session: session.scalar(select(ModelDispatchScope.scope))) is None


@pytest.mark.parametrize("case", ["cancel", "revoke"])
def test_waiting_for_spacing_rechecks_authority_and_releases_permits(client, monkeypatch, case):
    runtime = client.app.state.runtime
    entry(client, channel(client))
    pat = token(client, ["model:invoke"])
    model = ModelRuntime(internal_service(client, pat["token"]))
    scope = credential_scope(client)
    client.portal.call(dispatch(client).cooldown, scope, 3)
    waiting, drained = Event(), Event()
    original = ModelDispatchService.claim_in_transaction

    async def claim(self, session, key):
        delay = await original(self, session, key)
        if delay:
            waiting.set()
        return delay

    monkeypatch.setattr(ModelDispatchService, "claim_in_transaction", claim)

    async def exercise():
        try:
            await model.invoke(
                Invoke(model_key="chat", messages=[{"role": "user", "content": "x"}])
            )
        finally:
            drained.set()

    pending = client.portal.start_task_soon(exercise)
    assert waiting.wait(5)
    if case == "cancel":
        pending.cancel()
        with pytest.raises(CancelledError):
            pending.result(timeout=5)
    else:
        assert client.delete(f"/api/v1/auth/tokens/{pat['id']}").status_code == 204
        with pytest.raises(ServiceError) as error:
            pending.result(timeout=5)
        assert error.value.status in {401, 403}
    assert drained.wait(5)
    assert query(client, lambda session: session.scalar(select(ModelInvocation.id))) is None

    async def released():
        async with runtime.models.coordination.inference(scope) as permit:
            await permit()

    client.portal.call(released)


def test_two_real_workers_share_one_token_send_interval(client, monkeypatch, tmp_path):
    import json

    from support.operation_flow import PACKAGE, definition
    from test_flow import wait_run, worker

    runtime = client.app.state.runtime
    runtime.settings.flow_worker_concurrency = 1
    runtime.settings.model_send_interval_ms = 3000
    entry(client, channel(client), operations=["text.structured"])
    install_package(client, monkeypatch, PACKAGE)
    sent = tmp_path / "physical-sends.jsonl"
    released = tmp_path / "release-first"
    code = f"""import asyncio, json, os, time
from pathlib import Path
import httpx2
from keystone import composition
from support.operation_flow import PACKAGE
composition.FLOW_PACKAGES = (PACKAGE,)
composition.execution_version.cache_clear()
from keystone.platform.model_access.adapters.http import HttpClient
original = HttpClient.transport
def transport(self, provider):
    source = original(self, provider)
    async def send(request):
        if request.method == 'POST':
            with Path({str(sent)!r}).open('a') as output:
                output.write(json.dumps({{'worker': os.environ['KEYSTONE_WORKER_ID'],
                    'at': time.monotonic()}}) + '\\n')
            if os.environ['KEYSTONE_WORKER_ID'] == 'keystone-worker':
                async with asyncio.timeout(15):
                    while not Path({str(released)!r}).exists():
                        await asyncio.sleep(0.05)
            else:
                Path({str(released)!r}).touch()
        return await source.handle_async_request(request)
    return httpx2.MockTransport(send)
HttpClient.transport = transport
from keystone.entrypoints.worker import main
main()
"""

    def start(key):
        response = client.post(
            "/api/v1/_test/review",
            json={
                "definition_key": definition.key,
                "request_key": key,
                "input": {"prompt": "Answer"},
                "model_bindings": {"analyze": "chat"},
            },
        )
        assert response.status_code == 202, response.text
        return response.json()["id"]

    first = start("first")
    with worker(client, tmp_path, code=code) as (a, la):
        deadline = time.monotonic() + 15
        while not sent.exists():
            assert a.poll() is None and time.monotonic() < deadline, la.read_text()
            time.sleep(0.05)
        second = start("second")
        with worker(client, tmp_path, code=code, worker_id="keystone-worker-two") as (b, lb):
            done = wait_run(client, second, b, lb)
            assert done["status"] == "succeeded", (done, lb.read_text())
            done = wait_run(client, first, a, la)
            assert done["status"] == "succeeded", (done, la.read_text())
    attempts = [json.loads(line) for line in sent.read_text().splitlines()]
    assert [row["worker"] for row in attempts] == ["keystone-worker", "keystone-worker-two"]
    assert attempts[1]["at"] - attempts[0]["at"] >= 2.8
    rows = query(client, lambda s: s.scalars(select(ModelInvocation))).all()
    assert len(rows) == 2 and {row.worker_id for row in rows} == {
        "keystone-worker",
        "keystone-worker-two",
    }
    assert len(query(client, lambda s: s.scalars(select(ModelDispatchScope))).all()) == 1

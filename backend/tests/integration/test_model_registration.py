"""Commit ownership survives cancellation and cleanup errors, without a second send."""

import asyncio
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx2
import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from test_files_dictionary import client as client
from test_identity import query, token
from test_model_access import channel, entry, internal_service
from test_model_access import model_configuration as model_configuration

from keystone.core.errors import ServiceError
from keystone.platform.model_access.models import ModelInvocation
from keystone.platform.model_access.schemas import Invoke
from keystone.platform.model_access.services.runtime import ModelRuntime

pytestmark = pytest.mark.postgres


def prepare(client):
    entry(client, channel(client))
    return ModelRuntime(internal_service(client, token(client, ["model:invoke"])["token"]))


def request():
    return Invoke(model_key="chat", messages=[{"role": "user", "content": "x"}])


def test_model_admission_retries_before_registration_and_sends_exactly_once(client, monkeypatch):
    from support.model_stub import transport

    from keystone.core import retries

    runtime, model = client.app.state.runtime, prepare(client)
    identifier = uuid4()
    original = runtime.models.coordination.inference
    attempts, waits, sends = [], [], []

    @asynccontextmanager
    async def busy(scope):
        attempts.append(True)
        if len(attempts) < 3:
            async with runtime.database.transaction() as session:
                assert await session.get(ModelInvocation, identifier) is None
            raise ServiceError(409, "model_capacity_busy", "busy")
        async with original(scope) as permit:
            yield permit

    async def sleep(seconds):
        waits.append(seconds)

    class CountedTransport(httpx2.AsyncBaseTransport):
        async def handle_async_request(self, req):
            sends.append(True)
            return await transport("newapi").handle_async_request(req)

    monkeypatch.setattr(runtime.models.coordination, "inference", busy)
    monkeypatch.setattr(retries, "sleep", sleep)
    monkeypatch.setattr(runtime.models.http, "transport", lambda _: CountedTransport())
    result = client.portal.call(lambda: model.invoke(request(), invocation_id=identifier))
    assert result["content"] and waits == [3, 6] and len(attempts) == 3
    assert len(sends) == 1
    row = query(client, lambda session: session.get(ModelInvocation, identifier))
    assert row.status == "succeeded"
    with pytest.raises(ServiceError) as duplicate:
        client.portal.call(lambda: model.invoke(request(), invocation_id=identifier))
    assert duplicate.value.code == "model_result_unavailable" and len(sends) == 1


def test_model_admission_rechecks_revoked_pat_before_retry(client, monkeypatch):
    from sqlalchemy import update

    from keystone.core import retries
    from keystone.core.time import utcnow
    from keystone.platform.identity.models import PersonalAccessToken

    rt = client.app.state.runtime
    entry(client, channel(client))
    issued = token(client, ["model:invoke"])
    model = ModelRuntime(internal_service(client, issued["token"]))
    identifier, admissions = uuid4(), []

    @asynccontextmanager
    async def busy(_scope):
        admissions.append(True)
        raise ServiceError(409, "model_capacity_busy", "busy")
        yield  # pragma: no cover

    async def revoke(_seconds):
        async with rt.database.transaction() as session:
            await session.execute(
                update(PersonalAccessToken)
                .where(PersonalAccessToken.id == issued["id"])
                .values(revoked_at=utcnow())
            )

    async def forbidden(_request):
        pytest.fail("Revoked admission sent HTTP")

    monkeypatch.setattr(rt.models.coordination, "inference", busy)
    monkeypatch.setattr(rt.models.http, "transport", lambda _: httpx2.MockTransport(forbidden))
    monkeypatch.setattr(retries, "sleep", revoke)
    with pytest.raises(ServiceError) as denied:
        client.portal.call(lambda: model.invoke(request(), invocation_id=identifier))
    assert denied.value.status in {401, 403}
    assert len(admissions) == 1
    assert query(client, lambda session: session.get(ModelInvocation, identifier)) is None


@pytest.mark.parametrize(
    ("stage", "outcome"),
    [("session_close", "cancel"), ("session_close", "error"), ("commit_return", "cancel")],
)
def test_committed_registration_is_finished_when_cleanup_is_interrupted(
    client, monkeypatch, stage, outcome
):
    runtime, model = client.app.state.runtime, prepare(client)
    identifier = uuid4()
    entered, release = asyncio.Event(), asyncio.Event()
    marked, requests = [], []
    original_close, original_commit = AsyncSession.close, AsyncSession.commit

    def before_commit(session):
        if not marked and session.scalar(
            select(ModelInvocation.id).where(ModelInvocation.id == identifier)
        ):
            marked.append(True)
            session.info["test_registration"] = True

    async def pause():
        entered.set()
        await release.wait()
        if outcome == "error":
            raise RuntimeError("isolated-after-commit-failure")

    async def close(session):
        registration = session.info.pop("test_registration", False)
        try:
            if registration and stage == "session_close":
                await pause()
        finally:
            await original_close(session)

    async def commit(session):
        await original_commit(session)
        if session.info.get("test_registration") and stage == "commit_return":
            await pause()

    async def forbidden_send(_request):
        requests.append(True)
        raise AssertionError("An interrupted registration must never send HTTP")

    event.listen(Session, "before_commit", before_commit)
    monkeypatch.setattr(AsyncSession, "close", close)
    monkeypatch.setattr(AsyncSession, "commit", commit)
    monkeypatch.setattr(
        runtime.models.http, "transport", lambda _: httpx2.MockTransport(forbidden_send)
    )

    async def exercise():
        task = asyncio.create_task(model.invoke(request(), invocation_id=identifier))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            if outcome == "cancel":
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
                if stage == "commit_return":
                    assert not task.done()  # Commit drains before cancellation propagates.
            release.set()
            with pytest.raises(asyncio.CancelledError if outcome == "cancel" else ServiceError):
                await task
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        with pytest.raises(ServiceError) as duplicate:
            await model.invoke(request(), invocation_id=identifier)
        assert duplicate.value.code == "model_result_unavailable"

    try:
        client.portal.call(exercise)
    finally:
        event.remove(Session, "before_commit", before_commit)
    row = query(client, lambda session: session.get(ModelInvocation, identifier))
    assert row.status == "unknown" and row.finished_at is not None
    assert row.cost_amount is None and row.cost_source == "unknown"
    assert not requests

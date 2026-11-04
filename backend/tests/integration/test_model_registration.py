"""Commit ownership survives cancellation and cleanup errors, without a second send."""

import asyncio
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

"""Identity lock waits are bounded and cancelled waiters retain no locks or writes."""

import asyncio
from time import monotonic

import pytest
from sqlalchemy import event, text
from test_identity import client as client
from test_identity import create_user, login

from keystone.core.errors import ServiceError
from keystone.platform.identity.services.authorization import (
    IDENTITY_LOCK_TIMEOUT_SECONDS,
    identity_lock,
)

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("holder_write,waiter_write", [(False, True), (True, False)])
def test_conflicting_identity_wait_is_bounded_and_releases_connection(
    client, holder_write, waiter_write
):
    database = client.app.state.runtime.database

    async def exercise():
        async with database.transaction() as holder:
            await identity_lock(holder, identity_write=holder_write)
            started = monotonic()
            with pytest.raises(ServiceError) as rejected:
                async with database.transaction() as waiter:
                    await identity_lock(waiter, identity_write=waiter_write)
            assert rejected.value.code == "identity_busy" and rejected.value.status == 503
            assert monotonic() - started < IDENTITY_LOCK_TIMEOUT_SECONDS + 3
        async with database.transaction() as session:
            await identity_lock(session, identity_write=True)
            assert await session.scalar(text("SELECT 1")) == 1

    client.portal.call(exercise)


def test_cancelled_identity_waiter_is_removed_before_reuse(client):
    database = client.app.state.runtime.database

    async def exercise():
        async def waiting():
            async with database.transaction() as session:
                await identity_lock(session, identity_write=True)

        async with database.transaction() as holder:
            await identity_lock(holder, identity_write=True)
            attempted = asyncio.Event()

            def observed(connection, cursor, statement, parameters, context, many):
                if "pg_try_advisory_xact_lock(" in statement:
                    attempted.set()

            event.listen(database.engine.sync_engine, "after_cursor_execute", observed)
            task = asyncio.create_task(waiting())
            try:
                async with asyncio.timeout(2):
                    await attempted.wait()
                assert not task.done()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert (
                    await holder.scalar(
                        text(
                            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                            "AND objid = 73190421 AND NOT granted"
                        )
                    )
                    == 0
                )
            finally:
                event.remove(database.engine.sync_engine, "after_cursor_execute", observed)
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        async with database.transaction() as session:
            await identity_lock(session, identity_write=True)

    client.portal.call(exercise)


def test_identity_mutation_times_out_without_writing_and_can_retry(client):
    login(client)
    user = create_user(client)
    path = f"/api/v1/users/{user['id']}"

    async def exercise():
        async with client.app.state.runtime.database.transaction() as holder:
            await identity_lock(holder)
            response = await asyncio.to_thread(client.patch, path, json={"display_name": "changed"})
            assert response.status_code == 503 and response.json()["code"] == "identity_busy"

    client.portal.call(exercise)
    assert (
        next(u for u in client.get("/api/v1/users").json() if u["id"] == user["id"])["display_name"]
        == "alice"
    )
    assert client.patch(path, json={"display_name": "changed"}).status_code == 200

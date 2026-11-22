"""Session state changes shared by API requests and Worker maintenance.

Coordination lives in the database: the environment row lock serializes admission, the
session's operation lease marks a lifecycle call in flight, and the control columns name the
single input holder. Provider I/O always runs outside these short transactions.
"""

import asyncio
from datetime import timedelta

from sqlalchemy import update

from keystone.core.time import utcnow
from keystone.platform.audit.services.writer import record
from keystone.platform.environments.adapters.opensandbox import RuntimeUnavailable
from keystone.platform.environments.models import Environment, EnvironmentSession

OPERATION_LEASE = timedelta(minutes=5)
HUMAN_LEASE = timedelta(seconds=8)
CONTROL_KINDS = ("human", "automation")


def controlled(run, now=None):
    return run.control_owner is not None and run.control_until > (now or utcnow())


def operating(run, now=None):
    return run.operation_until is not None and run.operation_until > (now or utcnow())


def begin_operation(run):
    """Call in the transaction holding the environment lock; returns the lease to release."""
    run.operation_until = utcnow() + OPERATION_LEASE
    return run.operation_until


async def end_operation(database, run_id, lease):
    async with database.transaction() as session:
        await session.execute(
            update(EnvironmentSession)
            .where(EnvironmentSession.id == run_id, EnvironmentSession.operation_until == lease)
            .values(operation_until=None)
        )


def release_control(run, *, activity=True):
    run.control_owner = run.control_kind = run.control_until = None
    if activity:
        run.last_activity_at = utcnow()


async def save_session(
    database,
    row,
    actor,
    state,
    *,
    sandbox_id=None,
    delete_sent=False,
    audit=True,
    correlation_id=None,
):
    # Completion records external facts even if the original session is revoked meanwhile.
    async with database.transaction() as session:
        # Lock the environment before the session row, matching the request paths.
        environment = None
        if state == "ready":
            environment = await session.get(Environment, row.environment_id, with_for_update=True)
        current = await session.get(EnvironmentSession, row.id, with_for_update=True)
        if current.state != row.state:
            return current
        current.state = state
        if environment is not None:
            environment.state_initialization_pending = False
            current.last_activity_at = utcnow()
        elif current.control_owner is not None:
            # Only a ready session can be held; any other outcome ends the hold.
            release_control(current, activity=False)
            current.control_generation += 1
        if sandbox_id is not None:
            current.sandbox_id = sandbox_id
        if delete_sent:
            current.delete_sent = True
        if audit:
            await record(
                session,
                "environment.session.result",
                actor,
                "environment_session",
                row.id,
                correlation_id=correlation_id,
                details={"environment_id": str(row.environment_id), "state": state},
            )
        return current


async def wait_released(database, run_id, deadline):
    """Wait until the current holder lets go or its lease lapses; holders renew every 2s."""
    while True:
        async with database.transaction() as session:
            run = await session.get(EnvironmentSession, run_id)
            if not controlled(run):
                return
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError
        await asyncio.sleep(0.25)


async def quit_and_delete(
    database, runtime, run, actor, *, correlation_id=None, before_delete=None
):
    """Stop an accepted `stopping` session; returns the saved row (`stopped` or `review`)."""
    try:
        present = False
        async with asyncio.timeout(20):
            if await runtime.exists(run.sandbox_id):
                present = True
                await runtime.quit_browser(run.sandbox_id, run.id)
        if before_delete is not None:
            await before_delete(present)
        if present:
            async with asyncio.timeout(20):
                run = await save_session(
                    database, run, actor, "stopping", delete_sent=True, audit=False
                )
                await runtime.delete(run.sandbox_id)
    except (RuntimeUnavailable, TimeoutError):
        return await save_session(database, run, actor, "review", correlation_id=correlation_id)
    return await save_session(database, run, actor, "stopped", correlation_id=correlation_id)

"""Stop interactive sessions nobody has used for a while; run by Worker maintenance."""

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import or_, select

from keystone.core.time import utcnow
from keystone.platform.audit.services.writer import Actor, record
from keystone.platform.environments.models import Environment, EnvironmentSession
from keystone.platform.environments.services.lifecycle import (
    begin_operation,
    controlled,
    end_operation,
    operating,
    quit_and_delete,
)

IDLE_AFTER = timedelta(minutes=30)
KEEP_LIMIT = timedelta(hours=4)
KEEP_TIMEOUT = 1
BATCH = 10
log = logging.getLogger(__name__)


def _idle(run, cutoff, now):
    return (
        run.purpose == "interactive"
        and run.state == "ready"
        and not run.stop_requested
        and run.last_activity_at < cutoff
        and not controlled(run, now)
        and not operating(run, now)
    )


async def _kept(database, declared, environment_id, run_id, last_activity_at):
    """Ask the declaring business whether it still waits on this session; errors keep it."""
    if declared is None or declared.keep is None:
        return False
    try:
        async with asyncio.timeout(KEEP_TIMEOUT), database.transaction() as session:
            until = await declared.keep(session, environment_id, run_id)
    except Exception as error:
        log.warning("environment_keep_failed key=%s type=%s", declared.key, type(error).__name__)
        return True
    # A business cannot keep a sandbox running indefinitely past its last real use.
    return until is not None and min(until, last_activity_at + KEEP_LIMIT) > utcnow()


async def stop_idle(database, runtime, keys=None):
    """Accept and finish at most one batch; held, starting, busy or kept sessions are skipped."""
    keys = keys or {}
    now = utcnow()
    cutoff = now - IDLE_AFTER
    async with database.transaction() as session:
        candidates = list(
            await session.execute(
                select(
                    EnvironmentSession.id,
                    EnvironmentSession.environment_id,
                    EnvironmentSession.last_activity_at,
                    Environment.key,
                )
                .join(Environment, Environment.id == EnvironmentSession.environment_id)
                .where(
                    EnvironmentSession.purpose == "interactive",
                    EnvironmentSession.state == "ready",
                    EnvironmentSession.last_activity_at < cutoff,
                    or_(
                        EnvironmentSession.control_until.is_(None),
                        EnvironmentSession.control_until <= now,
                    ),
                )
                .order_by(EnvironmentSession.last_activity_at, EnvironmentSession.id)
                .limit(BATCH)
            )
        )
    stopped = 0
    for run_id, environment_id, last_activity_at, key in candidates:
        # Outside the environment lock: the business query must not hold up user requests.
        if await _kept(database, keys.get(key), environment_id, run_id, last_activity_at):
            continue
        async with database.transaction() as session:
            # Same lock order as user requests: environment, then session.
            await session.get(Environment, environment_id, with_for_update=True)
            run = await session.get(EnvironmentSession, run_id, with_for_update=True)
            if not _idle(run, cutoff, utcnow()):
                continue
            run.state, run.stop_requested = "stopping", True
            run.control_generation += 1
            lease = begin_operation(run)
            await record(
                session,
                "environment.session.stop",
                Actor("system"),
                "environment_session",
                run.id,
                details={"environment_id": str(environment_id), "state": run.state},
            )
        try:
            # Idle stops keep the volume and existing backups but take no new backup.
            await quit_and_delete(database, runtime, run, Actor("system"))
            stopped += 1
        finally:
            await end_operation(database, run.id, lease)
    return stopped

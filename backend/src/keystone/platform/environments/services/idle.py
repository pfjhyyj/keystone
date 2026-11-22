"""Stop interactive sessions nobody has used for a while; run by Worker maintenance."""

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
BATCH = 10


def _idle(run, cutoff, now):
    return (
        run.purpose == "interactive"
        and run.state == "ready"
        and not run.stop_requested
        and run.last_activity_at < cutoff
        and not controlled(run, now)
        and not operating(run, now)
    )


async def stop_idle(database, runtime):
    """Accept and finish at most one batch; held, starting or busy sessions are skipped."""
    now = utcnow()
    cutoff = now - IDLE_AFTER
    async with database.transaction() as session:
        candidates = list(
            await session.execute(
                select(EnvironmentSession.id, EnvironmentSession.environment_id)
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
    for run_id, environment_id in candidates:
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

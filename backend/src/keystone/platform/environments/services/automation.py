"""Business Flows drive an environment with bounded actions under their original identity.

A hold names the Flow run as owner. Each action is registered with the next sequence number
before it is sent, so a replayed Step finds its number taken and reports an unknown outcome
instead of sending the input again. Businesses choose actions and targets; the platform enforces
the hold, the action set and the declared domain scope.
"""

import json
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import select

from keystone.core.errors import ServiceError
from keystone.core.time import utcnow
from keystone.platform.environments.adapters.opensandbox import RuntimeUnavailable
from keystone.platform.environments.models import Environment
from keystone.platform.environments.schemas import OCCUPIED
from keystone.platform.environments.services.environments import EnvironmentService, _busy
from keystone.platform.environments.services.lifecycle import controlled, release_control

ACTIONS = {
    "chromium": frozenset(
        {"snapshot", "click", "type", "read", "clipboard", "screenshot", "wait", "open"}
    ),
}
RESULT_FIELDS = ("state", "url", "title", "elements", "frames", "text", "image")
AUTOMATION_LEASE = timedelta(seconds=60)
ACTION_MARGIN = timedelta(seconds=5)


@dataclass(frozen=True)
class Handover:
    """The session and control generation a user handed back; the next segment expects both."""

    session_id: UUID
    generation: int


@dataclass(frozen=True)
class AutomationGrant:
    environment_id: UUID
    session_id: UUID
    owner: UUID
    generation: int


def _lost():
    return ServiceError(409, "environment_control_lost", "自动控制已失效")


def _not_ready():
    return ServiceError(409, "environment_not_ready", "交互环境不可连接")


def _changed():
    return ServiceError(409, "environment_session_changed", "交互环境会话已变化，请重新处理")


def _in_scope(url, domains):
    host = (urlsplit(url).hostname or "").lower() if isinstance(url, str) else ""
    return url == "about:blank" or any(host == d or host.endswith("." + d) for d in domains)


class EnvironmentAutomation:
    def __init__(self, context, runtime, files, keys):
        self.context, self.runtime = context, runtime
        self.environments = EnvironmentService(context, runtime, files, keys, session_only=False)

    async def _keyed(self, session, auth, kind, key):
        """The caller's existing environment for a declared key, locked; never creates one."""
        declared = self.environments.keys.get(key)
        if declared is None or kind not in declared.kinds:
            raise ServiceError(404, "environment_key_not_declared", "交互环境未声明")
        return await session.scalar(
            select(Environment)
            .where(
                Environment.owner_id == auth.user.id,
                Environment.kind == kind,
                Environment.key == key,
            )
            .with_for_update()
        )

    async def handover(self, kind, key):
        """Record what the user hands back: the ready, unheld session and its generation."""
        async with self.context.access("environment:use") as (session, auth):
            environment = await self._keyed(session, auth, kind, key)
            if environment is None:
                raise _not_ready()
            run = await self.environments._latest_session(session, environment.id, lock=True)
            if run is None or run.purpose != "interactive" or run.state != "ready":
                raise _not_ready()
            if run.stop_requested:
                raise _not_ready()
            if controlled(run):
                raise _busy()
            return Handover(run.id, run.control_generation)

    async def acquire(self, kind, key, owner, expect=None):
        """Take the caller's environment for `owner`; resumes the owner's own hold.

        Without `expect` the environment is created and opened if needed. With a handover
        only that session is used, and only if nobody has held it since.
        """
        if expect is None:
            environment = await self.environments.ensure(kind, key)
            view = await self.environments.open(environment.id)
            if view.state != "ready":
                raise _not_ready()
            environment_id, session_id = environment.id, view.id
        else:
            environment_id, session_id = None, expect.session_id
        async with self.context.access(
            "environment:use",
            action="environment.automation.acquire",
            target="environment_session",
            target_id=session_id,
        ) as (session, auth):
            env = self.environments
            if expect is None:
                await env._environment(session, auth, environment_id, lock=True)
            else:
                environment = await self._keyed(session, auth, kind, key)
                if environment is None:
                    raise _changed()
                environment_id = environment.id
            try:
                run = await env._session_record(session, environment_id, session_id, lock=True)
            except ServiceError:
                if expect is None:
                    raise
                raise _changed() from None
            if expect is not None and (run.purpose != "interactive" or run.state not in OCCUPIED):
                raise _changed()
            if run.state != "ready" or run.stop_requested:
                raise _not_ready()
            now = utcnow()
            if controlled(run, now):
                # The owner's own hold survives a Worker restart; `expect` is not compared again.
                if run.control_owner != owner or run.control_kind != "automation":
                    raise _busy()
            else:
                if expect is not None and run.control_generation != expect.generation:
                    raise ServiceError(
                        409, "environment_control_changed", "交还之后环境被其他人操作过"
                    )
                run.control_generation += 1
                run.control_owner, run.control_kind = owner, "automation"
                run.control_sequence, run.control_busy_until = 0, None
                run.control_until = now + AUTOMATION_LEASE
                await self.context.log(
                    session,
                    auth,
                    "environment.automation.acquire",
                    "environment_session",
                    run.id,
                    {"environment_id": str(environment_id)},
                )
            run.control_until, run.last_activity_at = now + AUTOMATION_LEASE, now
            return AutomationGrant(environment_id, run.id, owner, run.control_generation)

    async def _locked(self, session, auth, grant):
        env = self.environments
        await env._environment(session, auth, grant.environment_id, lock=True)
        run = await env._session_record(session, grant.environment_id, grant.session_id, lock=True)
        if (
            run.state != "ready"
            or run.stop_requested
            or run.control_owner != grant.owner
            or run.control_kind != "automation"
            or run.control_generation != grant.generation
            or not controlled(run)
        ):
            raise _lost()
        return run

    async def renew(self, grant):
        async with self.context.access("environment:use") as (session, auth):
            run = await self._locked(session, auth, grant)
            now = utcnow()
            run.control_until = max(now + AUTOMATION_LEASE, run.control_busy_until or now)
            run.last_activity_at = now

    async def release(self, grant):
        """Hand input back; an action whose outcome is unknown keeps the hold until it expires."""
        async with self.context.access("environment:use") as (session, auth):
            try:
                run = await self._locked(session, auth, grant)
            except ServiceError:
                return
            if run.control_busy_until and run.control_busy_until > utcnow():
                run.control_until = run.control_busy_until
            else:
                release_control(run)

    async def act(self, grant, sequence, name, arguments=None, *, domains, timeout_seconds=15):
        """Send one action as number `sequence` of this hold and return its projected result."""
        arguments = arguments or {}
        domains = tuple(d.lower().strip(".") for d in domains)
        if (
            type(sequence) is not int
            or sequence < 1
            or not domains
            or not all(domains)
            or type(timeout_seconds) is not int
            or not 1 <= timeout_seconds <= 30
            or len(json.dumps(arguments)) > 4096
        ):
            raise ValueError("Invalid environment action")
        async with self.context.access("environment:use") as (session, auth):
            run = await self._locked(session, auth, grant)
            environment = await self.environments._environment(session, auth, grant.environment_id)
            if name not in ACTIONS[environment.kind]:
                raise ServiceError(422, "environment_action_unsupported", "交互环境不支持该动作")
            if sequence <= run.control_sequence:
                # Already registered by an earlier attempt: never send the same input twice.
                raise ServiceError(409, "environment_action_unknown", "动作结果未知，请先核对界面")
            if sequence != run.control_sequence + 1:
                raise ServiceError(409, "environment_action_sequence", "动作序号不连续")
            now = utcnow()
            run.control_sequence = sequence
            run.control_busy_until = now + timedelta(seconds=timeout_seconds) + ACTION_MARGIN
            run.control_until = run.control_busy_until + AUTOMATION_LEASE
            run.last_activity_at = now
            sandbox_id = run.sandbox_id
        try:
            result = await self.runtime.action(
                sandbox_id, grant.session_id, name, arguments, timeout_seconds
            )
        except (RuntimeUnavailable, TimeoutError):
            raise ServiceError(
                503, "environment_action_unknown", "动作结果未知，请先核对界面"
            ) from None
        async with self.context.access("environment:use") as (session, auth):
            run = await self._locked(session, auth, grant)
            if run.control_sequence == sequence:
                run.control_busy_until = None
            run.last_activity_at = utcnow()
            # Leaving the declared scope ends this hold; commit that before reporting it.
            escaped = not _in_scope(result.get("url"), domains)
            if escaped:
                release_control(run)
        if escaped:
            raise ServiceError(409, "environment_scope_violation", "页面已离开允许的范围")
        return {key: result[key] for key in RESULT_FIELDS if key in result}

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

from keystone.core.errors import ServiceError
from keystone.core.time import utcnow
from keystone.platform.environments.adapters.opensandbox import RuntimeUnavailable
from keystone.platform.environments.services.environments import EnvironmentService, _busy
from keystone.platform.environments.services.lifecycle import controlled, release_control

ACTIONS = {
    "chromium": frozenset({"snapshot", "click", "type", "read", "screenshot", "wait", "open"}),
}
RESULT_FIELDS = ("state", "url", "title", "elements", "text", "image")
AUTOMATION_LEASE = timedelta(seconds=60)
ACTION_MARGIN = timedelta(seconds=5)


@dataclass(frozen=True)
class AutomationGrant:
    environment_id: UUID
    session_id: UUID
    owner: UUID
    generation: int


def _lost():
    return ServiceError(409, "environment_control_lost", "自动控制已失效")


def _in_scope(url, domains):
    host = (urlsplit(url).hostname or "").lower() if isinstance(url, str) else ""
    return url == "about:blank" or any(host == d or host.endswith("." + d) for d in domains)


class EnvironmentAutomation:
    def __init__(self, context, runtime, files, keys):
        self.context, self.runtime = context, runtime
        self.environments = EnvironmentService(context, runtime, files, keys, session_only=False)

    async def acquire(self, kind, key, owner):
        """Take the caller's environment for `owner`, starting it if needed; resumes own hold."""
        environment = await self.environments.ensure(kind, key)
        view = await self.environments.open(environment.id)
        if view.state != "ready":
            raise ServiceError(409, "environment_not_ready", "交互环境不可连接")
        async with self.context.access(
            "environment:use",
            action="environment.automation.acquire",
            target="environment_session",
            target_id=view.id,
        ) as (session, auth):
            env = self.environments
            await env._environment(session, auth, environment.id, lock=True)
            run = await env._session_record(session, environment.id, view.id, lock=True)
            if run.state != "ready" or run.stop_requested:
                raise ServiceError(409, "environment_not_ready", "交互环境不可连接")
            now = utcnow()
            if controlled(run, now):
                if run.control_owner != owner or run.control_kind != "automation":
                    raise _busy()
            else:
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
                    {"environment_id": str(environment.id)},
                )
            run.control_until, run.last_activity_at = now + AUTOMATION_LEASE, now
            return AutomationGrant(environment.id, run.id, owner, run.control_generation)

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

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from keystone.core.errors import ServiceError
from keystone.core.time import utcnow
from keystone.platform.audit.services.writer import Actor
from keystone.platform.environments.adapters.opensandbox import CreateRejected, RuntimeUnavailable
from keystone.platform.environments.models import Environment, EnvironmentSession
from keystone.platform.environments.schemas import OCCUPIED, EnvironmentView, SessionView
from keystone.platform.environments.schemas.keys import DEFAULT
from keystone.platform.environments.services.backups import BrowserBackups
from keystone.platform.environments.services.lifecycle import (
    HUMAN_LEASE,
    begin_operation,
    controlled,
    end_operation,
    operating,
    quit_and_delete,
    release_control,
    save_session,
    wait_released,
)


@dataclass(frozen=True)
class ControlConnection:
    """One screen connection's hold; renewals must match both owner and generation."""

    id: UUID
    generation: int


def _view(run):
    return SessionView(
        id=run.id,
        purpose=run.purpose,
        state=run.state,
        can_stop=run.purpose == "interactive"
        and (
            run.state == "ready"
            or (run.state == "review" and bool(run.sandbox_id) and not run.delete_sent)
        ),
    )


def _busy():
    return ServiceError(409, "environment_in_use", "交互环境正在被其他页面或自动任务控制")


class EnvironmentService:
    def __init__(self, context, runtime, files, keys):
        self.context, self.runtime, self.keys = context, runtime, keys
        self.backups = BrowserBackups(context, runtime, files)

    def _require_runtime(self):
        if self.runtime is None:
            raise ServiceError(503, "environment_unconfigured", "交互环境提供方未配置")

    async def _environment(self, session, auth, environment_id, *, lock=False):
        query = select(Environment).where(
            Environment.id == environment_id, Environment.owner_id == auth.user.id
        )
        row = await session.scalar(query.with_for_update() if lock else query)
        if row is None:
            raise ServiceError(404, "environment_not_found", "交互环境不存在")
        return row

    async def _latest_session(self, session, environment_id, *, lock=False):
        query = (
            select(EnvironmentSession)
            .where(EnvironmentSession.environment_id == environment_id)
            .order_by(
                EnvironmentSession.state.in_(OCCUPIED).desc(),
                EnvironmentSession.created_at.desc(),
                EnvironmentSession.id.desc(),
            )
            .limit(1)
        )
        return await session.scalar(query.with_for_update() if lock else query)

    async def _session_record(self, session, environment_id, session_id, *, lock=False):
        row = await session.get(EnvironmentSession, session_id, with_for_update=lock)
        if row is None or row.environment_id != environment_id:
            raise ServiceError(404, "environment_session_not_found", "交互环境会话不存在")
        return row

    def _view_environment(self, environment, run=None):
        declared = self.keys.get(environment.key)
        return EnvironmentView(
            id=environment.id,
            kind=environment.kind,
            key=environment.key,
            name=declared.name if declared else environment.key,
            session=_view(run) if run else None,
            backup_at=environment.backup_at,
            backup_failed=environment.backup_failed,
            backup_enabled=self.backups.enabled,
        )

    async def listing(self, offset, limit):
        async with self.context.access("environment:use", session_only=True) as (session, auth):
            # The default environment is listed before its first use and shifts later pages.
            placeholder, first_page = None, offset == 0
            if self.runtime is not None and not await session.scalar(
                select(Environment.id).where(
                    Environment.owner_id == auth.user.id,
                    Environment.kind == "chromium",
                    Environment.key == DEFAULT.key,
                )
            ):
                placeholder = EnvironmentView(
                    id=None,
                    kind="chromium",
                    key=DEFAULT.key,
                    name=DEFAULT.name,
                    backup_enabled=self.backups.enabled,
                )
                if first_page:
                    limit -= 1
                else:
                    offset -= 1
            environments = await session.scalars(
                select(Environment)
                .where(Environment.owner_id == auth.user.id)
                .order_by(Environment.kind, Environment.key, Environment.id)
                .offset(offset)
                .limit(limit)
            )
            results = [placeholder] if placeholder and first_page else []
            for environment in environments:
                run = await self._latest_session(session, environment.id)
                results.append(self._view_environment(environment, run))
            return results

    async def ensure(self, kind, key):
        """Return the caller's environment for a declared key, creating it on first use."""
        declared = self.keys.get(key)
        if declared is None or kind not in declared.kinds:
            raise ServiceError(404, "environment_key_not_declared", "交互环境未声明")
        async with self.context.access("environment:use", session_only=True) as (session, auth):
            identifier = uuid4()
            created = await session.scalar(
                insert(Environment)
                .values(
                    id=identifier,
                    owner_id=auth.user.id,
                    kind=kind,
                    key=key,
                    volume=f"keystone-environment-{identifier}",
                    state_initialization_pending=True,
                )
                .on_conflict_do_nothing(constraint="uq_environment_owner_kind_key")
                .returning(Environment.id)
            )
            if created is not None:
                await self.context.log(
                    session,
                    auth,
                    "environment.create",
                    "environment",
                    created,
                    {"kind": kind, "key": key},
                )
            environment = await session.scalar(
                select(Environment).where(
                    Environment.owner_id == auth.user.id,
                    Environment.kind == kind,
                    Environment.key == key,
                )
            )
            run = await self._latest_session(session, environment.id)
            return self._view_environment(environment, run)

    async def _save(self, row, actor, state, **kwargs):
        return await save_session(
            self.context.database,
            row,
            Actor("user", actor),
            state,
            correlation_id=self.context.correlation_id,
            **kwargs,
        )

    async def open(self, environment_id):
        self._require_runtime()
        action = "environment.session.open"
        async with self.context.access(
            "environment:use",
            session_only=True,
            action=action,
            target="environment",
            target_id=environment_id,
        ) as (session, auth):
            environment = await self._environment(session, auth, environment_id, lock=True)
            run = await self._latest_session(session, environment_id, lock=True)
            if run and run.state in OCCUPIED:
                if run.state != "ready" or operating(run):
                    return _view(run)
                create = False
            else:
                create = True
                run = EnvironmentSession(
                    environment_id=environment_id,
                    purpose="interactive",
                    state="starting",
                    stop_requested=False,
                    delete_sent=False,
                )
                session.add(run)
                await session.flush()
                await self.context.log(
                    session,
                    auth,
                    action,
                    "environment_session",
                    run.id,
                    {"environment_id": str(environment_id), "state": run.state},
                )
            lease = begin_operation(run)
            actor = auth.user.id
        try:
            if not create:
                return _view(await self._observe(run, actor))
            try:
                async with asyncio.timeout(20):
                    sandbox_id = await self.runtime.create(
                        run.id,
                        environment_id,
                        environment.volume,
                        initialize=environment.state_initialization_pending,
                    )
                    run = await self._save(
                        run, actor, "starting", sandbox_id=sandbox_id, audit=False
                    )
                restored_from = None
                state = await self.runtime.wait_profile(sandbox_id, run.id)
                if state == "missing":
                    # A pending environment initializes itself; others never start empty.
                    if environment.state_initialization_pending:
                        raise ServiceError(
                            409, "environment_initialization_failed", "交互环境初始化失败"
                        )
                    if not self.backups.enabled:
                        raise ServiceError(
                            409, "environment_state_missing", "交互环境卷缺失，且未启用备份"
                        )
                    restored_from = await self.backups.restore(environment_id, run.id, sandbox_id)
                elif state != "present":
                    raise ServiceError(
                        409, "environment_volume_invalid", "交互环境卷无法读取，未覆盖原内容"
                    )
                await self.runtime.wait_ready(sandbox_id, run.id)
            except CreateRejected:
                return _view(await self._save(run, actor, "failed"))
            except ServiceError:
                await self._discard_start(run, actor)
                raise
            except (RuntimeUnavailable, TimeoutError):
                return _view(await self._save(run, actor, "review"))
            result = _view(await self._save(run, actor, "ready"))
            return result.model_copy(update={"restored_from": restored_from})
        finally:
            await end_operation(self.context.database, run.id, lease)

    async def _discard_start(self, run, actor):
        run = await self._save(run, actor, "starting", delete_sent=True, audit=False)
        try:
            await self.runtime.delete(run.sandbox_id)
        except (RuntimeUnavailable, TimeoutError):
            await self._save(run, actor, "review")
        else:
            await self._save(run, actor, "failed")

    async def backup(self, environment_id):
        self._require_runtime()
        if not self.backups.enabled:
            raise ServiceError(503, "environment_backup_unconfigured", "交互环境备份未配置")
        async with self.context.access("environment:use", session_only=True) as (session, auth):
            auth.require("environment:stop")
            environment = await self._environment(session, auth, environment_id, lock=True)
            run = await self._latest_session(session, environment_id)
            if run and run.state in OCCUPIED:
                raise ServiceError(409, "environment_busy", "请先结束会话再备份")
            actor = auth.user.id
            run = EnvironmentSession(
                environment_id=environment_id,
                purpose="backup",
                state="starting",
                stop_requested=True,
                delete_sent=False,
            )
            session.add(run)
            await session.flush()
            lease = begin_operation(run)
        try:
            try:
                sandbox_id = await self.runtime.create(
                    run.id, environment_id, environment.volume, purpose="backup"
                )
                run = await self._save(run, actor, "starting", sandbox_id=sandbox_id, audit=False)
                state = await self.runtime.wait_profile(sandbox_id, run.id)
                if state == "present":
                    success = await self.backups.capture(environment_id, run.id, sandbox_id, actor)
                else:
                    await self.backups.failure(environment_id, actor)
                    success = False
                run = await self._save(run, actor, "starting", delete_sent=True, audit=False)
                await self.runtime.delete(sandbox_id)
            except CreateRejected:
                await self.backups.failure(environment_id, actor)
                await self._save(run, actor, "failed")
                raise ServiceError(
                    503, "environment_backup_failed", "未能生成备份，已有备份仍保留"
                ) from None
            except (RuntimeUnavailable, TimeoutError):
                await self.backups.failure(environment_id, actor)
                await self._save(run, actor, "review")
                raise ServiceError(
                    503, "environment_backup_review", "备份会话需要重新核对"
                ) from None
            await self._save(run, actor, "stopped")
            if not success:
                raise ServiceError(503, "environment_backup_failed", "未能生成备份，已有备份仍保留")
            return {"status": "ready"}
        finally:
            await end_operation(self.context.database, run.id, lease)

    async def check(self, environment_id, session_id):
        self._require_runtime()
        action = "environment.session.check"
        async with self.context.access(
            "environment:use",
            session_only=True,
            action=action,
            target="environment_session",
            target_id=session_id,
        ) as (session, auth):
            await self._environment(session, auth, environment_id, lock=True)
            run = await self._session_record(session, environment_id, session_id, lock=True)
            if run.state not in OCCUPIED or operating(run):
                return _view(run)
            actor = auth.user.id
            await self.context.log(
                session,
                auth,
                action,
                "environment_session",
                run.id,
                {"environment_id": str(environment_id), "state": run.state},
            )
            lease = begin_operation(run)
        try:
            return _view(await self._observe(run, actor))
        finally:
            await end_operation(self.context.database, run.id, lease)

    async def _observe(self, run, actor):
        sandbox_id, state = run.sandbox_id, "review"
        try:
            async with asyncio.timeout(8):
                if sandbox_id:
                    if not await self.runtime.exists(sandbox_id):
                        state = "stopped"
                else:
                    # An empty search cannot prove that a timed-out create has ended.
                    sandbox_id = await self.runtime.find(run.id, run.environment_id)
                if (
                    state != "stopped"
                    and sandbox_id
                    and run.purpose == "interactive"
                    and not run.stop_requested
                    and not run.delete_sent
                ):
                    if await self.runtime.ready(sandbox_id, run.id):
                        state = "ready"
        except (RuntimeUnavailable, TimeoutError):
            state = "review"
        if state == run.state and sandbox_id == run.sandbox_id:
            return run
        # Leaving `ready` ends any hold; the holder notices on its next renewal.
        return await self._save(run, actor, state, sandbox_id=sandbox_id)

    async def stop(self, environment_id, session_id):
        self._require_runtime()
        action = "environment.session.stop"
        async with self.context.access(
            "environment:use",
            session_only=True,
            action=action,
            target="environment_session",
            target_id=session_id,
        ) as (session, auth):
            auth.require("environment:stop")
            await self._environment(session, auth, environment_id, lock=True)
            run = await self._session_record(session, environment_id, session_id, lock=True)
            if operating(run) or run.purpose != "interactive" or not _view(run).can_stop:
                return _view(run)
            if controlled(run) and run.control_kind == "automation":
                raise _busy()
            actor = auth.user.id
            run.state, run.stop_requested = "stopping", True
            # A new generation invalidates the screen hold; it drains on its next renewal.
            run.control_generation += 1
            await self.context.log(
                session,
                auth,
                action,
                "environment_session",
                run.id,
                {"environment_id": str(environment_id), "state": run.state},
            )
            lease = begin_operation(run)
        try:
            try:
                await wait_released(
                    self.context.database, run.id, asyncio.get_running_loop().time() + 12
                )
            except TimeoutError:
                return _view(await self._save(run, actor, "review"))

            async def backup(present):
                if not self.backups.enabled:
                    return
                if present:
                    await self.backups.capture(environment_id, run.id, run.sandbox_id, actor)
                else:
                    await self.backups.failure(environment_id, actor)

            return _view(
                await quit_and_delete(
                    self.context.database,
                    self.runtime,
                    run,
                    Actor("user", actor),
                    correlation_id=self.context.correlation_id,
                    before_delete=backup,
                )
            )
        finally:
            await end_operation(self.context.database, run.id, lease)

    async def _admit(self, environment_id, session_id, connection=None):
        """Grant a screen hold, or renew one; both recheck the login session and the run."""
        action = None if connection else "environment.connection.open"
        async with self.context.access(
            "environment:use",
            session_only=True,
            action=action,
            target="environment_session",
            target_id=session_id,
        ) as (session, auth):
            await self._environment(session, auth, environment_id, lock=True)
            run = await self._session_record(session, environment_id, session_id, lock=True)
            if (
                run.purpose != "interactive"
                or run.state != "ready"
                or run.stop_requested
                or not run.sandbox_id
            ):
                raise ServiceError(409, "environment_not_ready", "交互环境不可连接")
            now = utcnow()
            if connection is None:
                if controlled(run, now):
                    raise _busy()
                run.control_generation += 1
                connection = ControlConnection(uuid4(), run.control_generation)
                run.control_owner, run.control_kind = connection.id, "human"
                run.control_until, run.last_activity_at = now + HUMAN_LEASE, now
                await self.context.log(
                    session,
                    auth,
                    action,
                    "environment_session",
                    session_id,
                    {"environment_id": str(environment_id)},
                )
            elif (
                run.control_owner != connection.id
                or run.control_generation != connection.generation
            ):
                raise ServiceError(409, "environment_control_lost", "画面控制已被收回")
            run.control_until = now + HUMAN_LEASE
            return connection, run.sandbox_id, auth.credential.expires_at

    async def renew(self, environment_id, session_id, connection):
        _, _, expiry = await self._admit(environment_id, session_id, connection)
        return None, expiry

    async def _release(self, session_id, connection):
        async with self.context.database.transaction() as session:
            run = await session.get(EnvironmentSession, session_id, with_for_update=True)
            # The owner ID is unique per connection, so a stop's new generation still releases.
            if run is not None and run.control_owner == connection.id:
                release_control(run)

    @asynccontextmanager
    async def control(self, environment_id, session_id):
        self._require_runtime()
        connection, sandbox_id, expiry = await self._admit(environment_id, session_id)
        try:
            yield connection, sandbox_id, expiry
        finally:
            # Finish the release even if the socket task is cancelled; the lease is the fallback.
            await asyncio.shield(self._release(session_id, connection))

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import select

from keystone.core.errors import ServiceError
from keystone.platform.audit.services.writer import Actor, record
from keystone.platform.browser.adapters.opensandbox import CreateRejected, RuntimeUnavailable
from keystone.platform.browser.models import BrowserRun, BrowserWorkspace
from keystone.platform.browser.schemas import OCCUPIED, RunView, WorkspaceView
from keystone.platform.browser.services.backups import WorkspaceBackups


@dataclass
class ControlConnection:
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    closed: asyncio.Event = field(default_factory=asyncio.Event)


class BrowserControls:
    """All control and lifecycle entrypoints run in one Keystone API process."""

    def __init__(self):
        self.gate = asyncio.Lock()
        self.operations: set[UUID] = set()
        self.connections: dict[UUID, ControlConnection] = {}


def _view(run):
    return RunView(
        id=run.id,
        purpose=run.purpose,
        state=run.state,
        can_stop=run.purpose == "browse"
        and (
            run.state == "ready"
            or (run.state == "review" and bool(run.sandbox_id) and not run.delete_sent)
        ),
    )


class BrowserService:
    def __init__(self, context, runtime, controls, files):
        self.context, self.runtime, self.controls = context, runtime, controls
        self.backups = WorkspaceBackups(context, runtime, files)

    def _require_runtime(self):
        if self.runtime is None:
            raise ServiceError(503, "browser_unconfigured", "浏览器运行环境未配置")

    async def _workspace(self, session, auth, workspace_id, *, lock=False):
        query = select(BrowserWorkspace).where(
            BrowserWorkspace.id == workspace_id, BrowserWorkspace.owner_id == auth.user.id
        )
        row = await session.scalar(query.with_for_update() if lock else query)
        if row is None:
            raise ServiceError(404, "browser_workspace_not_found", "浏览器空间不存在")
        return row

    async def _latest_run(self, session, workspace_id):
        return await session.scalar(
            select(BrowserRun)
            .where(BrowserRun.workspace_id == workspace_id)
            .order_by(
                BrowserRun.state.in_(OCCUPIED).desc(),
                BrowserRun.created_at.desc(),
                BrowserRun.id.desc(),
            )
            .limit(1)
        )

    async def _run(self, session, workspace_id, run_id):
        row = await session.get(BrowserRun, run_id)
        if row is None or row.workspace_id != workspace_id:
            raise ServiceError(404, "browser_run_not_found", "浏览器运行不存在")
        return row

    async def listing(self, offset, limit):
        async with self.context.access("browser:use", session_only=True) as (session, auth):
            workspaces = await session.scalars(
                select(BrowserWorkspace)
                .where(BrowserWorkspace.owner_id == auth.user.id)
                .order_by(BrowserWorkspace.name, BrowserWorkspace.id)
                .offset(offset)
                .limit(limit)
            )
            results = []
            for workspace in workspaces:
                run = await self._latest_run(session, workspace.id)
                results.append(
                    WorkspaceView(
                        id=workspace.id,
                        name=workspace.name,
                        run=_view(run) if run else None,
                        backup_at=workspace.backup_at,
                        backup_failed=workspace.backup_failed,
                        backup_enabled=self.backups.enabled,
                    )
                )
            return results

    async def _save(self, row, actor, state, *, sandbox_id=None, delete_sent=False, audit=True):
        # Completion records external facts even if the original session is revoked meanwhile.
        async with self.context.database.transaction() as session:
            current = await session.get(BrowserRun, row.id, with_for_update=True)
            if current.state != row.state:
                return current
            current.state = state
            if sandbox_id is not None:
                current.sandbox_id = sandbox_id
            if delete_sent:
                current.delete_sent = True
            if audit:
                await record(
                    session,
                    "browser.run.result",
                    Actor("user", actor),
                    "browser_run",
                    row.id,
                    correlation_id=self.context.correlation_id,
                    details={"workspace_id": str(row.workspace_id), "state": state},
                )
            return current

    async def open(self, workspace_id):
        self._require_runtime()
        action = "browser.run.open"
        async with self.controls.gate:
            async with self.context.access(
                "browser:use",
                session_only=True,
                action=action,
                target="browser_workspace",
                target_id=workspace_id,
            ) as (session, auth):
                workspace = await self._workspace(session, auth, workspace_id, lock=True)
                run = await self._latest_run(session, workspace_id)
                if run and run.state in OCCUPIED:
                    if run.state != "ready" or run.id in self.controls.operations:
                        return _view(run)
                    create = False
                else:
                    create = True
                    run = BrowserRun(
                        workspace_id=workspace_id,
                        purpose="browse",
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
                        "browser_run",
                        run.id,
                        {"workspace_id": str(workspace_id), "state": run.state},
                    )
                actor = auth.user.id
            self.controls.operations.add(run.id)
        try:
            if not create:
                return _view(await self._observe(run, actor))
            try:
                async with asyncio.timeout(20):
                    sandbox_id = await self.runtime.create(run.id, workspace_id, workspace.volume)
                    run = await self._save(
                        run, actor, "starting", sandbox_id=sandbox_id, audit=False
                    )
                restored_from = None
                if self.backups.enabled:
                    state = await self.runtime.wait_profile(sandbox_id, run.id)
                    if state == "missing":
                        restored_from = await self.backups.restore(workspace_id, run.id, sandbox_id)
                    elif state != "present":
                        raise ServiceError(
                            409, "browser_volume_invalid", "浏览器空间卷无法读取，未覆盖原内容"
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
            self.controls.operations.discard(run.id)

    async def _discard_start(self, run, actor):
        run = await self._save(run, actor, "starting", delete_sent=True, audit=False)
        try:
            await self.runtime.delete(run.sandbox_id)
        except (RuntimeUnavailable, TimeoutError):
            await self._save(run, actor, "review")
        else:
            await self._save(run, actor, "failed")

    async def backup(self, workspace_id):
        self._require_runtime()
        if not self.backups.enabled:
            raise ServiceError(503, "browser_backup_unconfigured", "浏览器备份未配置")
        async with self.controls.gate:
            async with self.context.access("browser:use", session_only=True) as (session, auth):
                auth.require("browser:stop")
                workspace = await self._workspace(session, auth, workspace_id, lock=True)
                run = await self._latest_run(session, workspace_id)
                if run and run.state in OCCUPIED:
                    raise ServiceError(409, "browser_workspace_busy", "请先停止浏览器再备份")
                actor = auth.user.id
                run = BrowserRun(
                    workspace_id=workspace_id,
                    purpose="backup",
                    state="starting",
                    stop_requested=True,
                    delete_sent=False,
                )
                session.add(run)
                await session.flush()
            self.controls.operations.add(run.id)
        try:
            try:
                sandbox_id = await self.runtime.create(
                    run.id, workspace_id, workspace.volume, purpose="backup"
                )
                run = await self._save(run, actor, "starting", sandbox_id=sandbox_id, audit=False)
                state = await self.runtime.wait_profile(sandbox_id, run.id)
                if state == "present":
                    success = await self.backups.capture(workspace_id, run.id, sandbox_id, actor)
                else:
                    await self.backups.failure(workspace_id, actor)
                    success = False
                run = await self._save(run, actor, "starting", delete_sent=True, audit=False)
                await self.runtime.delete(sandbox_id)
            except CreateRejected:
                await self.backups.failure(workspace_id, actor)
                await self._save(run, actor, "failed")
                raise ServiceError(
                    503, "browser_backup_failed", "未能生成备份，已有备份仍保留"
                ) from None
            except (RuntimeUnavailable, TimeoutError):
                await self.backups.failure(workspace_id, actor)
                await self._save(run, actor, "review")
                raise ServiceError(503, "browser_backup_review", "备份运行需要重新核对") from None
            await self._save(run, actor, "stopped")
            if not success:
                raise ServiceError(503, "browser_backup_failed", "未能生成备份，已有备份仍保留")
            return {"status": "ready"}
        finally:
            self.controls.operations.discard(run.id)

    async def check(self, workspace_id, run_id):
        self._require_runtime()
        action = "browser.run.check"
        async with self.controls.gate:
            async with self.context.access(
                "browser:use",
                session_only=True,
                action=action,
                target="browser_run",
                target_id=run_id,
            ) as (session, auth):
                await self._workspace(session, auth, workspace_id, lock=True)
                run = await self._run(session, workspace_id, run_id)
                if run.state not in OCCUPIED or run.id in self.controls.operations:
                    return _view(run)
                actor = auth.user.id
                await self.context.log(
                    session,
                    auth,
                    action,
                    "browser_run",
                    run.id,
                    {"workspace_id": str(workspace_id), "state": run.state},
                )
            self.controls.operations.add(run.id)
        try:
            return _view(await self._observe(run, actor))
        finally:
            self.controls.operations.discard(run.id)

    async def _observe(self, run, actor):
        sandbox_id, state = run.sandbox_id, "review"
        try:
            async with asyncio.timeout(8):
                if sandbox_id:
                    if not await self.runtime.exists(sandbox_id):
                        state = "stopped"
                else:
                    # An empty search cannot prove that a timed-out create has ended.
                    sandbox_id = await self.runtime.find(run.id, run.workspace_id)
                if (
                    state != "stopped"
                    and sandbox_id
                    and run.purpose == "browse"
                    and not run.stop_requested
                    and not run.delete_sent
                ):
                    if await self.runtime.ready(sandbox_id, run.id):
                        state = "ready"
        except (RuntimeUnavailable, TimeoutError):
            state = "review"
        if state == run.state and sandbox_id == run.sandbox_id:
            return run
        async with self.controls.gate:
            if state != "ready":
                if connection := self.controls.connections.get(run.id):
                    connection.stop.set()
            return await self._save(run, actor, state, sandbox_id=sandbox_id)

    async def stop(self, workspace_id, run_id):
        self._require_runtime()
        action = "browser.run.stop"
        async with self.controls.gate:
            async with self.context.access(
                "browser:use",
                session_only=True,
                action=action,
                target="browser_run",
                target_id=run_id,
            ) as (session, auth):
                auth.require("browser:stop")
                await self._workspace(session, auth, workspace_id, lock=True)
                run = await self._run(session, workspace_id, run_id)
                if (
                    run.id in self.controls.operations
                    or run.purpose != "browse"
                    or not _view(run).can_stop
                ):
                    return _view(run)
                actor = auth.user.id
                run.state, run.stop_requested = "stopping", True
                await self.context.log(
                    session,
                    auth,
                    action,
                    "browser_run",
                    run.id,
                    {"workspace_id": str(workspace_id), "state": run.state},
                )
            self.controls.operations.add(run.id)
            connection = self.controls.connections.get(run.id)
            if connection:
                connection.stop.set()
        try:
            try:
                present = False
                async with asyncio.timeout(20):
                    if connection:
                        await connection.closed.wait()
                    if await self.runtime.exists(run.sandbox_id):
                        present = True
                        await self.runtime.quit_browser(run.sandbox_id, run.id)
                if self.backups.enabled:
                    if present:
                        await self.backups.capture(workspace_id, run.id, run.sandbox_id, actor)
                    else:
                        await self.backups.failure(workspace_id, actor)
                if present:
                    async with asyncio.timeout(20):
                        run = await self._save(
                            run, actor, "stopping", delete_sent=True, audit=False
                        )
                        await self.runtime.delete(run.sandbox_id)
            except (RuntimeUnavailable, TimeoutError):
                return _view(await self._save(run, actor, "review"))
            return _view(await self._save(run, actor, "stopped"))
        finally:
            self.controls.operations.discard(run.id)

    async def stream_authorized(self, workspace_id, run_id, *, initial=False):
        action = "browser.connection.open" if initial else None
        async with self.context.access(
            "browser:use",
            session_only=True,
            action=action,
            target="browser_run",
            target_id=run_id,
        ) as (session, auth):
            await self._workspace(session, auth, workspace_id)
            run = await self._run(session, workspace_id, run_id)
            if run.purpose != "browse" or run.state != "ready" or not run.sandbox_id:
                raise ServiceError(409, "browser_not_ready", "浏览器不可连接")
            if initial:
                if run_id in self.controls.connections:
                    raise ServiceError(409, "browser_in_use", "浏览器正在被其他页面控制")
                await self.context.log(
                    session,
                    auth,
                    action,
                    "browser_run",
                    run_id,
                    {"workspace_id": str(workspace_id)},
                )
            return run.sandbox_id, auth.credential.expires_at

    @asynccontextmanager
    async def control(self, workspace_id, run_id):
        self._require_runtime()
        connection = ControlConnection()
        async with self.controls.gate:
            sandbox_id, expiry = await self.stream_authorized(workspace_id, run_id, initial=True)
            self.controls.connections[run_id] = connection
        try:
            yield connection, sandbox_id, expiry
        finally:
            self.controls.connections.pop(run_id)
            connection.closed.set()

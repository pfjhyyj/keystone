import asyncio
import hmac
from uuid import uuid4

from sqlalchemy import select

from keystone.core.errors import ServiceError
from keystone.core.time import utcnow
from keystone.platform.audit.services.writer import Actor, record
from keystone.platform.browser.adapters.opensandbox import RuntimeUnavailable
from keystone.platform.browser.models import BrowserWorkspace
from keystone.platform.files.schemas import Upload
from keystone.platform.files.services.references import FileReferences, registered_file_ids

SLOT = "latest_backup"


class WorkspaceBackups:
    def __init__(self, context, runtime, files):
        self.context, self.runtime, self.files = context, runtime, files

    @property
    def enabled(self):
        return self.context.settings.browser_backup_key is not None

    def _key(self, workspace_id):
        master = bytes.fromhex(self.context.settings.browser_backup_key.get_secret_value())
        return hmac.digest(
            master, b"keystone.browser.backup.v1:" + workspace_id.bytes, "sha256"
        ).hex()

    async def capture(self, workspace_id, run_id, sandbox_id, actor):
        captured_at = utcnow()
        settings = self.context.settings
        try:
            async with asyncio.timeout(150):
                async with self.runtime.backup(
                    sandbox_id,
                    run_id,
                    self._key(workspace_id),
                    settings.file_max_bytes,
                ) as (size, checksum, chunks):
                    file = await self.files.upload_stream(
                        Upload(
                            storage_key=settings.file_default_storage,
                            name="browser-workspace.ksbr",
                            expected_size=size,
                            checksum_sha256=checksum,
                            request_key=f"browser-backup:{uuid4()}",
                        ),
                        chunks,
                    )
                async with self.context.access("browser:use", session_only=True) as (session, auth):
                    auth.require("browser:stop")
                    workspace = await session.scalar(
                        select(BrowserWorkspace)
                        .where(
                            BrowserWorkspace.id == workspace_id,
                            BrowserWorkspace.owner_id == auth.user.id,
                        )
                        .with_for_update()
                    )
                    if workspace is None:
                        raise ServiceError(404, "browser_workspace_not_found", "浏览器空间不存在")
                    refs = FileReferences(
                        session, auth, "browser", correlation_id=self.context.correlation_id
                    )
                    await refs.replace(str(workspace_id), SLOT, (file["id"],))
                    workspace.backup_at, workspace.backup_failed = captured_at, False
                    await self._audit(session, workspace_id, actor, "ready")
            return True
        except (RuntimeUnavailable, ServiceError, TimeoutError, OSError):
            await self.failure(workspace_id, actor)
            return False

    async def failure(self, workspace_id, actor):
        async with self.context.database.transaction() as session:
            workspace = await session.get(BrowserWorkspace, workspace_id, with_for_update=True)
            workspace.backup_failed = True
            await self._audit(session, workspace_id, actor, "failed")

    async def _audit(self, session, workspace_id, actor, state):
        await record(
            session,
            "browser.workspace.backup",
            Actor("user", actor),
            "browser_workspace",
            workspace_id,
            correlation_id=self.context.correlation_id,
            details={"state": state},
        )

    async def restore(self, workspace_id, run_id, sandbox_id):
        async with asyncio.timeout(150):
            async with self.context.access("browser:use", session_only=True) as (session, auth):
                workspace = await session.scalar(
                    select(BrowserWorkspace).where(
                        BrowserWorkspace.id == workspace_id,
                        BrowserWorkspace.owner_id == auth.user.id,
                    )
                )
                if workspace is None:
                    raise ServiceError(404, "browser_workspace_not_found", "浏览器空间不存在")
                ids = await registered_file_ids(session, f"browser:{workspace_id}", SLOT)
                if workspace.backup_at is None or len(ids) != 1:
                    raise ServiceError(
                        409, "browser_backup_missing", "浏览器空间卷缺失，且没有可用备份"
                    )
                actor, captured_at = auth.user.id, workspace.backup_at
            async with self.files.open_content(
                ids[0], max_bytes=self.context.settings.file_max_bytes
            ) as reader:
                if not await self.runtime.restore(
                    sandbox_id, run_id, self._key(workspace_id), reader.size, reader.chunks()
                ):
                    raise ServiceError(409, "browser_restore_failed", "备份无法校验或恢复")
            async with self.context.database.transaction() as session:
                await record(
                    session,
                    "browser.workspace.restore",
                    Actor("user", actor),
                    "browser_workspace",
                    workspace_id,
                    correlation_id=self.context.correlation_id,
                    details={"backup_at": captured_at.isoformat()},
                )
            return captured_at

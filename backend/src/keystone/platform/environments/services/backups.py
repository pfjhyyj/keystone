import asyncio
import hmac
from uuid import uuid4

from sqlalchemy import select

from keystone.core.errors import ServiceError
from keystone.core.time import utcnow
from keystone.platform.audit.services.writer import Actor, record
from keystone.platform.environments.adapters.opensandbox import RuntimeUnavailable
from keystone.platform.environments.models import Environment
from keystone.platform.files.schemas import Upload
from keystone.platform.files.services.references import FileReferences, registered_file_ids

SLOT = "latest_backup"


class BrowserBackups:
    def __init__(self, context, runtime, files):
        self.context, self.runtime, self.files = context, runtime, files

    @property
    def enabled(self):
        return self.context.settings.browser_backup_key is not None

    def _key(self, environment_id):
        master = bytes.fromhex(self.context.settings.browser_backup_key.get_secret_value())
        return hmac.digest(
            master, b"keystone.browser.backup.v1:" + environment_id.bytes, "sha256"
        ).hex()

    async def capture(self, environment_id, session_id, sandbox_id, actor):
        captured_at = utcnow()
        settings = self.context.settings
        try:
            async with asyncio.timeout(150):
                async with self.runtime.backup(
                    sandbox_id,
                    session_id,
                    self._key(environment_id),
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
                async with self.context.access("environment:use", session_only=True) as (
                    session,
                    auth,
                ):
                    auth.require("environment:stop")
                    environment = await session.scalar(
                        select(Environment)
                        .where(
                            Environment.id == environment_id,
                            Environment.owner_id == auth.user.id,
                        )
                        .with_for_update()
                    )
                    if environment is None:
                        raise ServiceError(404, "environment_not_found", "交互环境不存在")
                    refs = FileReferences(
                        session, auth, "environment", correlation_id=self.context.correlation_id
                    )
                    await refs.replace(str(environment_id), SLOT, (file["id"],))
                    environment.backup_at, environment.backup_failed = captured_at, False
                    await self._audit(session, environment_id, actor, "ready")
            return True
        except (RuntimeUnavailable, ServiceError, TimeoutError, OSError):
            await self.failure(environment_id, actor)
            return False

    async def failure(self, environment_id, actor):
        async with self.context.database.transaction() as session:
            environment = await session.get(Environment, environment_id, with_for_update=True)
            environment.backup_failed = True
            await self._audit(session, environment_id, actor, "failed")

    async def _audit(self, session, environment_id, actor, state):
        await record(
            session,
            "environment.backup",
            Actor("user", actor),
            "environment",
            environment_id,
            correlation_id=self.context.correlation_id,
            details={"state": state},
        )

    async def restore(self, environment_id, session_id, sandbox_id):
        async with asyncio.timeout(150):
            async with self.context.access("environment:use", session_only=True) as (session, auth):
                environment = await session.scalar(
                    select(Environment).where(
                        Environment.id == environment_id,
                        Environment.owner_id == auth.user.id,
                    )
                )
                if environment is None:
                    raise ServiceError(404, "environment_not_found", "交互环境不存在")
                ids = await registered_file_ids(session, f"environment:{environment_id}", SLOT)
                if environment.backup_at is None or len(ids) != 1:
                    raise ServiceError(
                        409, "environment_backup_missing", "交互环境卷缺失，且没有可用备份"
                    )
                actor, captured_at = auth.user.id, environment.backup_at
            async with self.files.open_content(
                ids[0], max_bytes=self.context.settings.file_max_bytes
            ) as reader:
                if not await self.runtime.restore(
                    sandbox_id, session_id, self._key(environment_id), reader.size, reader.chunks()
                ):
                    raise ServiceError(409, "environment_restore_failed", "备份无法校验或恢复")
            async with self.context.database.transaction() as session:
                await record(
                    session,
                    "environment.restore",
                    Actor("user", actor),
                    "environment",
                    environment_id,
                    correlation_id=self.context.correlation_id,
                    details={"backup_at": captured_at.isoformat()},
                )
            return captured_at

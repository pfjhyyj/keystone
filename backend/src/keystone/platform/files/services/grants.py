from sqlalchemy import select

from keystone.core.errors import ServiceError
from keystone.platform.files import policy
from keystone.platform.files.models import File, FileGrant
from keystone.platform.files.schemas import grant_view
from keystone.platform.identity.access import AccessContext
from keystone.platform.identity.models import Role, User


class FileGrantService:
    def __init__(self, identity: AccessContext):
        self.identity = identity

    async def ensure_role_unused(self, session, identifier):
        """Caller holds the exclusive identity lock through role deletion and commit."""
        if await session.scalar(
            select(FileGrant.id).where(FileGrant.role_id == identifier).limit(1)
        ):
            raise ServiceError(409, "role_in_use", "角色仍被文件授权引用，请先撤销授权")

    async def listing(self, identifier, offset=0, limit=50):
        async with self.identity.access("file:manage") as (session, auth):
            await policy.require(session, auth, await session.get(File, identifier), "manage")
            return [
                grant_view(row)
                for row in await session.scalars(
                    select(FileGrant)
                    .where(FileGrant.file_id == identifier)
                    .order_by(FileGrant.id)
                    .offset(offset)
                    .limit(limit)
                )
            ]

    async def grant(self, identifier, data=None, *, grant_id=None):
        async with self.identity.access(
            "file:manage", action="file.grant", target="file", target_id=identifier
        ) as (session, auth):
            return await self.grant_in_transaction(
                session, auth, identifier, data, grant_id=grant_id
            )

    async def grant_in_transaction(self, session, auth, identifier, data=None, *, grant_id=None):
        """Trusted caller supplies current auth from AccessContext.access() in this transaction."""
        auth.require("file:manage")
        file = await policy.locked(session, identifier)
        await policy.require(session, auth, file, "manage")
        # Revocation also allowed for failed/uploading files, but deletion owns its grants.
        if data:
            policy.editable(file)
            if data.user_id:
                await self.identity.existing(session, User, data.user_id)
            if data.role_id:
                await self.identity.existing(session, Role, data.role_id)
            row = await session.scalar(
                select(FileGrant).where(
                    FileGrant.file_id == identifier,
                    FileGrant.subject_type == data.subject_type,
                    FileGrant.user_id == data.user_id,
                    FileGrant.role_id == data.role_id,
                    FileGrant.action == data.action,
                )
            )
            if row:
                return grant_view(row)
            row = FileGrant(file_id=identifier, **data.model_dump(), created_by=auth.user.id)
            session.add(row)
            await session.flush()
        else:
            if file.status in {"deleting", "deleted"}:
                raise ServiceError(409, "file_unavailable", "删除期间不能修改授权")
            row = await session.get(FileGrant, grant_id)
            if row is None or row.file_id != identifier:
                raise ServiceError(404, "not_found", "授权不存在")
            await session.delete(row)
        details = {
            "grant_id": str(row.id),
            "subject_type": row.subject_type,
            "user_id": str(row.user_id) if row.user_id else None,
            "role_id": str(row.role_id) if row.role_id else None,
            "action": row.action,
            "added": data is not None,
        }
        await self.identity.log(session, auth, "file.grant", "file", identifier, details)
        return grant_view(row)

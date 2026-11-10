import re

from sqlalchemy.dialects.postgresql import insert

from keystone.core.errors import ServiceError
from keystone.platform.audit.services.writer import Actor, record
from keystone.platform.browser.models import BrowserWorkspace
from keystone.platform.identity.services.subjects import users_in_transaction


async def register_workspace(database, owner_id, name, volume):
    name = name.strip()
    if not 1 <= len(name) <= 128 or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}", volume):
        raise ServiceError(422, "invalid_browser_workspace", "空间名称或卷名称无效")
    async with database.transaction() as session:
        if not await users_in_transaction(session, [owner_id]):
            raise ServiceError(404, "browser_owner_not_found", "浏览器空间所有者不存在")
        identifier = await session.scalar(
            insert(BrowserWorkspace)
            .values(owner_id=owner_id, name=name, volume=volume)
            .on_conflict_do_nothing(index_elements=[BrowserWorkspace.volume])
            .returning(BrowserWorkspace.id)
        )
        if identifier is None:
            raise ServiceError(409, "browser_volume_registered", "此卷已有浏览器空间")
        await record(
            session,
            "browser.workspace.register",
            Actor("system"),
            "browser_workspace",
            identifier,
            details={"owner_id": str(owner_id)},
        )
        return identifier

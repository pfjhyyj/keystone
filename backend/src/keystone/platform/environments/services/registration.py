import re

from sqlalchemy.dialects.postgresql import insert

from keystone.core.errors import ServiceError
from keystone.platform.audit.services.writer import Actor, record
from keystone.platform.environments.models import Environment
from keystone.platform.identity.services.subjects import users_in_transaction


async def register_environment(database, owner_id, name, volume):
    name = name.strip()
    if not 1 <= len(name) <= 128 or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}", volume):
        raise ServiceError(422, "invalid_environment", "环境名称或卷名称无效")
    async with database.transaction() as session:
        if not await users_in_transaction(session, [owner_id]):
            raise ServiceError(404, "environment_owner_not_found", "交互环境所有者不存在")
        identifier = await session.scalar(
            insert(Environment)
            .values(owner_id=owner_id, kind="chromium", name=name, volume=volume)
            .on_conflict_do_nothing(index_elements=[Environment.volume])
            .returning(Environment.id)
        )
        if identifier is None:
            raise ServiceError(409, "environment_volume_registered", "此卷已登记为交互环境")
        await record(
            session,
            "environment.register",
            Actor("system"),
            "environment",
            identifier,
            details={"owner_id": str(owner_id)},
        )
        return identifier

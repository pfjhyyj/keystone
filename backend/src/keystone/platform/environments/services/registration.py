import re

from sqlalchemy.dialects.postgresql import insert

from keystone.core.errors import ServiceError
from keystone.platform.audit.services.writer import Actor, record
from keystone.platform.environments.models import Environment
from keystone.platform.identity.services.subjects import users_in_transaction


async def register_environment(database, keys, owner_id, kind, key, volume):
    declared = keys.get(key)
    if declared is None or kind not in declared.kinds:
        raise ServiceError(404, "environment_key_not_declared", "交互环境未声明")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}", volume):
        raise ServiceError(422, "invalid_environment", "卷名称无效")
    async with database.transaction() as session:
        if not await users_in_transaction(session, [owner_id]):
            raise ServiceError(404, "environment_owner_not_found", "交互环境所有者不存在")
        identifier = await session.scalar(
            insert(Environment)
            .values(owner_id=owner_id, kind=kind, key=key, volume=volume)
            .on_conflict_do_nothing()
            .returning(Environment.id)
        )
        if identifier is None:
            raise ServiceError(409, "environment_volume_registered", "此卷或该用户的同一环境已登记")
        await record(
            session,
            "environment.register",
            Actor("system"),
            "environment",
            identifier,
            details={"owner_id": str(owner_id), "kind": kind, "key": key},
        )
        return identifier

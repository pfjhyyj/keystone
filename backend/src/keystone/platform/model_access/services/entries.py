from sqlalchemy import and_, false, or_, select

from keystone.core.errors import ServiceError
from keystone.platform.identity.access import AccessContext
from keystone.platform.model_access import schemas
from keystone.platform.model_access.gateway import LIVE_TARGETS
from keystone.platform.model_access.models import Channel, ModelEntry
from keystone.platform.model_access.services.queries import locked


class ModelEntryService:
    def __init__(self, identity: AccessContext, gateway):
        self.identity, self.gateway = identity, gateway

    async def catalog(self, offset, limit):
        return {
            "mode": self.gateway.settings.model_mode,
            "items": await self.entries(offset, limit, catalog=True),
        }

    async def prepare_invoke(self, model_key):
        selected = await self.selection(model_key)
        if self.gateway.settings.model_mode == "disabled":
            raise ServiceError(503, "model_transport_disabled", "模型发送已停用")
        return selected

    async def entries(self, offset, limit, *, catalog=False):
        async with self.identity.access("model:invoke" if catalog else "model:manage") as (
            session,
            _,
        ):
            statement = select(ModelEntry)
            if catalog:
                statement = statement.join(Channel).where(
                    ModelEntry.is_active.is_(True), Channel.is_active.is_(True)
                )
                if self.gateway.settings.model_mode == "live":
                    statement = statement.where(
                        or_(
                            false(),
                            *(
                                and_(Channel.provider == provider, Channel.api_base == base)
                                for provider, (base, paths) in LIVE_TARGETS.items()
                                if paths.get("/chat/completions") == "POST"
                            ),
                        )
                    )
            rows = await session.scalars(
                statement.order_by(ModelEntry.key).offset(offset).limit(limit)
            )
            return [
                {"key": row.key, "capabilities": ["text", "stream"]}
                if catalog
                else schemas.entry_view(row)
                for row in rows
            ]

    async def save_entry(self, data, identifier=None):
        action = "model.entry.update" if identifier else "model.entry.create"
        async with self.identity.access(
            "model:manage", action=action, target="model.entry", target_id=identifier
        ) as (session, auth):
            row = await locked(session, ModelEntry, identifier) if identifier else None
            await locked(session, Channel, data.channel_id)
            if row:
                before = {
                    key: str(getattr(row, key)) if key == "channel_id" else getattr(row, key)
                    for key in data.model_dump()
                }
                for key, value in data.model_dump().items():
                    setattr(row, key, value)
                self.identity.touch(row, auth)
            else:
                before = None
                row = ModelEntry(
                    **data.model_dump(), created_by=auth.user.id, updated_by=auth.user.id
                )
                session.add(row)
            await session.flush()
            await self.identity.log(
                session,
                auth,
                action,
                "model.entry",
                row.id,
                {"before": before, "after": data.model_dump(mode="json")},
            )
            return schemas.entry_view(row)

    async def selection(self, key):
        async with self.identity.access("model:invoke") as (session, _):
            return await select_model(session, key)


async def select_model(session, key, *, lock=False):
    """Trusted internal selection; caller owns authorization and transaction."""
    query = select(ModelEntry).where(ModelEntry.key == key)
    entry = await session.scalar(query.with_for_update(read=True) if lock else query)
    if entry is None:
        raise ServiceError(404, "model_not_found", "模型配置不存在")
    channel = await session.get(Channel, entry.channel_id)
    if channel is None or not entry.is_active or not channel.is_active:
        raise ServiceError(409, "model_disabled", "模型或渠道已停用")
    return schemas.Selection(
        model_entry_id=entry.id,
        channel_id=channel.id,
        provider_model=entry.provider_model,
        api_base=channel.api_base,
    )

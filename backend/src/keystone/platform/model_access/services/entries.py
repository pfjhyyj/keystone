from sqlalchemy import and_, false, or_, select

from keystone.core.errors import ServiceError
from keystone.platform.identity.services.access import AccessContext
from keystone.platform.model_access import schemas
from keystone.platform.model_access.adapters.channels import CHANNELS
from keystone.platform.model_access.models import Channel, ModelEntry
from keystone.platform.model_access.schemas.operations import Delivery, Operation
from keystone.platform.model_access.services.queries import locked


def catalog_entry(row, provider):
    adapter = CHANNELS.get(provider)
    operations = sorted(
        operation
        for operation in row.operations
        if adapter is not None and adapter.protocol(operation, model=row.provider_model) is not None
    )
    return {
        "key": row.key,
        "operations": operations,
        "capabilities": ["text", "stream"] if "text.generate" in operations else [],
    }


class ModelEntryService:
    def __init__(self, identity: AccessContext, clients):
        self.identity, self.clients = identity, clients

    async def catalog(
        self, offset, limit, *, operation: Operation = "text.generate", delivery: Delivery = "sync"
    ):
        return {
            "items": await self.entries(
                offset, limit, catalog=True, operation=operation, delivery=delivery
            ),
        }

    async def prepare_invoke(self, model_key):
        return await self.selection(model_key)

    async def entries(
        self, offset, limit, *, catalog=False, operation="text.generate", delivery="sync"
    ):
        async with self.identity.access("model:invoke" if catalog else "model:manage") as (
            session,
            _,
        ):
            statement = select(ModelEntry)
            if catalog:
                statement = statement.join(Channel).where(
                    ModelEntry.is_active.is_(True),
                    Channel.is_active.is_(True),
                    ModelEntry.operations.contains([operation]),
                )
                statement = statement.where(
                    or_(
                        false(),
                        *(
                            and_(
                                Channel.provider == provider,
                                Channel.api_base == adapter.api_base,
                                ModelEntry.provider_model.in_(protocol.models)
                                if protocol.models is not None
                                else True,
                            )
                            for provider, adapter in CHANNELS.items()
                            if (protocol := adapter.protocol(operation, delivery)) is not None
                        ),
                    )
                )
            rows = await session.scalars(
                statement.order_by(ModelEntry.key).offset(offset).limit(limit)
            )
            rows = list(rows)
            available = {}
            if catalog:
                for channel in await session.scalars(
                    select(Channel).where(Channel.id.in_({row.channel_id for row in rows}))
                ):
                    available[channel.id] = channel.provider
            return [
                catalog_entry(row, available[row.channel_id])
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
            values = data.model_dump()
            if values.get("operations") is None:
                values.pop("operations", None)
            if row:
                before = {
                    key: str(getattr(row, key)) if key == "channel_id" else getattr(row, key)
                    for key in values
                }
                for key, value in values.items():
                    setattr(row, key, value)
                self.identity.touch(row, auth)
            else:
                before = None
                row = ModelEntry(**values, created_by=auth.user.id, updated_by=auth.user.id)
                session.add(row)
            await session.flush()
            await self.identity.log(
                session,
                auth,
                action,
                "model.entry",
                row.id,
                {
                    "before": before,
                    "after": {
                        "channel_id": str(row.channel_id),
                        "provider_model": row.provider_model,
                        "is_active": row.is_active,
                        "operations": row.operations,
                        **({"key": row.key} if identifier is None else {}),
                    },
                },
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
    if "text.generate" not in entry.operations:
        raise ServiceError(409, "model_operation_unavailable", "模型不支持所需操作")
    return schemas.Selection(
        model_entry_id=entry.id,
        channel_id=channel.id,
        provider_model=entry.provider_model,
        api_base=channel.api_base,
        provider=channel.provider,
    )


async def resolve_bindings_in_transaction(session, auth, *, roles, bindings):
    """Caller owns identity transaction. Acquire every Entry before any Channel."""
    from keystone.platform.model_access.schemas.invocations import ResolvedModelBinding

    if {r.key for r in roles} != set(bindings):
        raise ServiceError(422, "flow_invalid_model_bindings", "模型角色绑定不完整或包含未知角色")
    if not roles:
        return {}
    auth.require("model:invoke")
    entries = list(
        await session.scalars(
            select(ModelEntry)
            .where(ModelEntry.key.in_(set(bindings.values())))
            .order_by(ModelEntry.id)
            .with_for_update(read=True)
        )
    )
    by_key = {entry.key: entry for entry in entries}
    if by_key.keys() != set(bindings.values()):
        raise ServiceError(409, "model_disabled", "模型或渠道不可用")
    channels = list(
        await session.scalars(
            select(Channel)
            .where(Channel.id.in_({e.channel_id for e in entries}))
            .order_by(Channel.id)
            .with_for_update(read=True)
        )
    )
    by_id = {channel.id: channel for channel in channels}
    result = {}
    for role in roles:
        entry = by_key[bindings[role.key]]
        channel = by_id.get(entry.channel_id)
        if channel is None or not channel.is_active or not entry.is_active:
            raise ServiceError(409, "model_disabled", "模型或渠道不可用")
        adapter = CHANNELS.get(channel.provider)
        if (
            adapter is None
            or channel.api_base != adapter.api_base
            or not adapter.supports(role.operation, role.delivery, model=entry.provider_model)
            or role.operation not in entry.operations
        ):
            raise ServiceError(409, "model_provider_unavailable", "模型不支持所需操作")
        result[role.key] = ResolvedModelBinding(
            model_key=entry.key,
            selection=schemas.Selection(
                model_entry_id=entry.id,
                channel_id=channel.id,
                provider_model=entry.provider_model,
                api_base=channel.api_base,
                provider=channel.provider,
                operation=role.operation,
                delivery=role.delivery,
            ),
        ).model_dump(mode="json")
    return result

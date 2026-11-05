"""Flat dictionaries. Business callers supply their own authorized transaction."""

from uuid import UUID

from sqlalchemy import select

from keystone.core.errors import ServiceError
from keystone.platform.dictionary.models import Dictionary, DictionaryItem
from keystone.platform.dictionary.schemas import ItemCreate, ItemEdit, item_view, view
from keystone.platform.identity.services.access import AccessContext


async def locked_dictionary(session, *, identifier=None, key=None):
    # Every selection/edit locks parent first, then item. Lock lives through business commit.
    row = await session.scalar(
        select(Dictionary)
        .where(Dictionary.id == identifier if identifier is not None else Dictionary.key == key)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise ServiceError(404, "dictionary_not_found", "字典不存在")
    return row


async def options(session, key, *, offset=0, limit=100):
    """Business entry authorizes and fixes key; public API separately requires read."""
    parent = await session.scalar(select(Dictionary).where(Dictionary.key == key))
    if parent is None:
        raise ServiceError(404, "dictionary_not_found", "字典不存在")
    if not parent.is_active:
        return []
    rows = await session.scalars(
        select(DictionaryItem)
        .where(DictionaryItem.dictionary_id == parent.id, DictionaryItem.is_active.is_(True))
        .order_by(DictionaryItem.sort_order, DictionaryItem.id)
        .offset(offset)
        .limit(limit)
    )
    return [item_view(row) for row in rows]


async def resolve(session, key, identifier):
    """Resolve only IDs already owned by an authorized business record, including disabled."""
    row = await session.scalar(
        select(DictionaryItem)
        .join(Dictionary)
        .where(Dictionary.key == key, DictionaryItem.id == identifier)
    )
    if row is None:
        if await session.scalar(select(Dictionary.id).where(Dictionary.key == key)) is None:
            raise ServiceError(404, "dictionary_not_found", "字典不存在")
        raise ServiceError(422, "dictionary_item_mismatch", "选项不存在或不属于指定字典")
    parent = await session.get(Dictionary, row.dictionary_id)
    return {**item_view(row), "dictionary_is_active": parent.is_active}


async def describe_items_in_transaction(session, keys, identifiers):
    """Read current items in caller-authorized dictionary scopes, including disabled."""
    keys, ids = set(keys), {UUID(str(value)) for value in identifiers}
    if len(keys) > 32 or len(ids) > 500:
        raise ValueError("Dictionary lookup range is too large")
    rows = await session.execute(
        select(DictionaryItem, Dictionary)
        .join(Dictionary, DictionaryItem.dictionary_id == Dictionary.id)
        .where(Dictionary.key.in_(keys), DictionaryItem.id.in_(ids))
        .order_by(Dictionary.key, DictionaryItem.id)
    )
    return [
        {**item_view(item), "dictionary_key": parent.key, "dictionary_is_active": parent.is_active}
        for item, parent in rows
    ]


async def validate_selection(session, key, identifier, *, previous_id=None):
    """previous_id must come from the locked business row, never the request payload."""
    parent = await locked_dictionary(session, key=key)
    item = await session.get(DictionaryItem, identifier, populate_existing=True)
    if item is None or item.dictionary_id != parent.id:
        raise ServiceError(422, "dictionary_item_mismatch", "选项不存在或不属于指定字典")
    if identifier != previous_id and not (parent.is_active and item.is_active):
        raise ServiceError(409, "dictionary_item_disabled", "该选项已停用，请重新选择")
    return item.id


class DictionaryService:
    def __init__(self, identity: AccessContext):
        self.identity = identity

    async def save(self, data, *, identifier: UUID | None = None, parent_id: UUID | None = None):
        item = isinstance(data, (ItemCreate, ItemEdit))
        model = DictionaryItem if item else Dictionary
        action = (
            f"dictionary.{'item' if item else 'definition'}.{'update' if identifier else 'create'}"
        )
        async with self.identity.access(
            "dictionary:manage",
            action=action,
            target="dictionary.item" if item else "dictionary",
            target_id=identifier,
        ) as (session, auth):
            if item:
                await locked_dictionary(session, identifier=parent_id)
            if identifier:
                row = (
                    await session.get(model, identifier, populate_existing=True)
                    if item
                    else await locked_dictionary(session, identifier=identifier)
                )
                if row is None or (item and row.dictionary_id != parent_id):
                    raise ServiceError(404, "not_found", "记录不存在")
                values = data.model_dump()
                before = {key: getattr(row, key) for key in values}
                for key, value in values.items():
                    setattr(row, key, value)
                self.identity.touch(row, auth)
            else:
                before = None
                row = model(**data.model_dump(), created_by=auth.user.id, updated_by=auth.user.id)
                if item:
                    row.dictionary_id = parent_id
                session.add(row)
            await session.flush()
            await self.identity.log(
                session,
                auth,
                action,
                "dictionary.item" if item else "dictionary",
                row.id,
                {"before": before, "after": data.model_dump()},
            )
            return item_view(row) if item else view(row)

    async def listing(self, offset, limit):
        async with self.identity.access("dictionary:read") as (session, _):
            return [
                view(row)
                for row in await session.scalars(
                    select(Dictionary).order_by(Dictionary.key).offset(offset).limit(limit)
                )
            ]

    async def items(self, identifier, offset, limit):
        async with self.identity.access("dictionary:read") as (session, _):
            await self.identity.existing(session, Dictionary, identifier)
            return [
                item_view(row)
                for row in await session.scalars(
                    select(DictionaryItem)
                    .where(DictionaryItem.dictionary_id == identifier)
                    .order_by(DictionaryItem.sort_order, DictionaryItem.id)
                    .offset(offset)
                    .limit(limit)
                )
            ]

    async def options(self, key, offset, limit):
        async with self.identity.access("dictionary:read") as (session, _):
            return await options(session, key, offset=offset, limit=limit)

    async def resolve(self, key, identifier):
        async with self.identity.access("dictionary:read") as (session, _):
            return await resolve(session, key, identifier)

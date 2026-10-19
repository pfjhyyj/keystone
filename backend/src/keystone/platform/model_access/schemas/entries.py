from uuid import UUID

from keystone.core.schemas import Input

from .types import Key, ModelName


class EntryEdit(Input):
    channel_id: UUID
    provider_model: ModelName
    is_active: bool = True


class EntryCreate(EntryEdit):
    key: Key


def entry_view(row):
    values = {
        key: getattr(row, key)
        for key in (
            "id",
            "key",
            "channel_id",
            "provider_model",
            "is_active",
            "created_at",
            "created_by",
            "updated_at",
            "updated_by",
        )
    }
    return values

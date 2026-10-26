from uuid import UUID

from pydantic import Field, field_validator

from keystone.core.schemas import Input

from .operations import Operation
from .types import Key, Label, ModelName


class NameEdit(Input):
    name: Label


class EntryEdit(Input):
    name: Label | None = None
    channel_id: UUID
    provider_model: ModelName
    is_active: bool = True
    operations: list[Operation] | None = Field(default=None, min_length=1, max_length=6)

    @field_validator("operations")
    @classmethod
    def unique_operations(cls, value):
        if value is None or len(value) != len(set(value)):
            raise ValueError("Model operations must be nonempty and unique")
        return sorted(value)


class EntryCreate(EntryEdit):
    key: Key
    operations: list[Operation] = Field(
        default_factory=lambda: ["text.generate"], min_length=1, max_length=6
    )


def entry_view(row):
    values = {
        key: getattr(row, key)
        for key in (
            "id",
            "key",
            "channel_id",
            "provider_model",
            "is_active",
            "operations",
            "created_at",
            "created_by",
            "updated_at",
            "updated_by",
        )
    }
    return {**values, "name": row.name or row.key}

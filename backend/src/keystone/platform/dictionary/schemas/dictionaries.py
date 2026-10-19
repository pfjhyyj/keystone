from pydantic import Field

from keystone.core.schemas import Input

from .types import Key, Label


class DictionaryCreate(Input):
    key: Key
    name: Label
    description: str | None = Field(None, max_length=1000)


class DictionaryEdit(Input):
    name: Label
    description: str | None = Field(None, max_length=1000)
    is_active: bool


def view(row):
    return {
        key: getattr(row, key)
        for key in (
            "id",
            "key",
            "name",
            "description",
            "is_active",
            "created_at",
            "created_by",
            "updated_at",
            "updated_by",
        )
    }

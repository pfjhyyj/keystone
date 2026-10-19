from pydantic import Field

from keystone.core.schemas import Input

from .types import Key, Label


class ItemCreate(Input):
    value: Key
    label: Label
    sort_order: int = Field(0, ge=-(2**31), le=2**31 - 1)


class ItemEdit(Input):
    label: Label
    sort_order: int = Field(0, ge=-(2**31), le=2**31 - 1)
    is_active: bool


def item_view(row):
    return {
        key: getattr(row, key)
        for key in (
            "id",
            "dictionary_id",
            "value",
            "label",
            "sort_order",
            "is_active",
            "created_at",
            "created_by",
            "updated_at",
            "updated_by",
        )
    }

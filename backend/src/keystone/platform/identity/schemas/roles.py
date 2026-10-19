from typing import Annotated

from pydantic import Field, StringConstraints

from keystone.core.schemas import Input

from .types import Name


class RoleUpdate(Input):
    name: Name
    description: str = Field(default="", max_length=1000)
    permissions: set[str] = Field(max_length=100)


class RoleCreate(RoleUpdate):
    key: Annotated[
        str, StringConstraints(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.-]*$")
    ]


class Assignment(Input):
    assigned: bool


def role_view(role, permissions):
    fields = (
        "id",
        "key",
        "name",
        "description",
        "created_at",
        "created_by",
        "updated_at",
        "updated_by",
    )
    return {
        **{key: getattr(role, key) for key in fields},
        "permissions": sorted(permissions),
    }

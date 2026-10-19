from keystone.core.schemas import Input

from .types import Name, Password, Username


class UserCreate(Input):
    username: Username
    display_name: Name
    password: Password


class UserUpdate(Input):
    display_name: Name


class ActiveChange(Input):
    is_active: bool


class PasswordReset(Input):
    password: Password


def user_view(user, role_ids=None):
    fields = (
        "id",
        "username",
        "display_name",
        "is_active",
        "password_change_required",
        "created_at",
        "created_by",
        "updated_at",
        "updated_by",
    )
    view = {key: getattr(user, key) for key in fields}
    if role_ids is not None:
        view["role_ids"] = list(role_ids)
    return view

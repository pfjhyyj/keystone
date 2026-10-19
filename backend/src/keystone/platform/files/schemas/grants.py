from typing import Literal
from uuid import UUID

from pydantic import model_validator

from keystone.core.schemas import Input


class Grant(Input):
    subject_type: Literal["user", "role", "authenticated"]
    user_id: UUID | None = None
    role_id: UUID | None = None
    action: Literal["inspect", "content_read", "manage"]

    @model_validator(mode="after")
    def valid_subject(self):
        valid = (
            (self.subject_type == "user" and self.user_id and not self.role_id)
            or (self.subject_type == "role" and self.role_id and not self.user_id)
            or (
                self.subject_type == "authenticated"
                and not self.user_id
                and not self.role_id
                and self.action != "manage"
            )
        )
        if not valid:
            raise ValueError("Invalid grant subject")
        return self


def grant_view(grant):
    return {
        key: getattr(grant, key)
        for key in (
            "id",
            "file_id",
            "subject_type",
            "user_id",
            "role_id",
            "action",
            "created_at",
            "created_by",
        )
    }

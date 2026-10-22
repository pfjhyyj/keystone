"""Ether's explicit per-file grants, with Keystone's administrator exceptions."""

from sqlalchemy import exists, literal, or_, select

from keystone.core.errors import ServiceError
from keystone.platform.files.models import File, FileGrant
from keystone.platform.identity.models import UserRole


def scope(auth, action):
    if auth.administrator and action in {"inspect", "manage"}:
        return literal(True)
    return exists(
        select(FileGrant.id).where(
            FileGrant.file_id == File.id,
            FileGrant.action == action,
            or_(
                FileGrant.user_id == auth.user.id,
                FileGrant.role_id.in_(
                    select(UserRole.role_id).where(UserRole.user_id == auth.user.id)
                ),
                FileGrant.subject_type == "authenticated",
            ),
        )
    )


async def require(session, auth, file, action):
    auth.require(f"file:{action}")
    if file is None or not await session.scalar(
        select(File.id).where(File.id == file.id, scope(auth, action))
    ):
        raise ServiceError(404, "file_not_found", "文件不存在或不可访问")
    if action == "content_read" and file.status != "active":
        raise ServiceError(409, "file_unavailable", "文件当前不可读取")


async def locked(session, identifier):
    row = await session.scalar(
        select(File)
        .where(File.id == identifier)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise ServiceError(404, "file_not_found", "文件不存在或不可访问")
    return row


def editable(file):
    if file.status not in {"active", "disabled"}:
        raise ServiceError(409, "file_unavailable", "仅可修改可用或停用的文件")

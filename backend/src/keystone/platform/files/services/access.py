from sqlalchemy import select

from keystone.core.errors import ServiceError
from keystone.platform.files.models import File


def available(file, *, content=False):
    """Technical availability only; the caller separately authorizes the exact File ID."""
    if file is None:
        raise ServiceError(404, "file_not_found", "文件不存在或不可访问")
    if content and file.status != "active":
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

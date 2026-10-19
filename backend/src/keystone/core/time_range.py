from datetime import timedelta

from sqlalchemy import func, select

from keystone.core.errors import ServiceError


async def history_range(session, since=None, until=None, *, error_code="invalid_time_range"):
    """Inclusive bounds; default to the 30 days ending at the database clock."""
    if until is None:
        until = await session.scalar(select(func.clock_timestamp()))
    if since is None:
        since = until - timedelta(days=30)
    if (
        since.utcoffset() is None
        or until.utcoffset() is None
        or not timedelta(0) <= until - since <= timedelta(days=366)
    ):
        raise ServiceError(422, error_code, "时间范围需带时区且不超过 366 天")
    return since, until

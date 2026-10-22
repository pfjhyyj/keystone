"""Public subject validation within the caller's identity-locked transaction."""

from keystone.core.errors import ServiceError
from keystone.platform.identity.models import Role, User


async def require_subjects(session, *, user_id=None, role_id=None):
    """Check existence, including disabled users; never commit or return ORM entities.

    The caller holds AccessContext.access() and its identity lock through commit,
    so role deletion cannot race a new reference. Authorization belongs to the use case.
    """
    for model, identifier in ((User, user_id), (Role, role_id)):
        if identifier is not None and await session.get(model, identifier) is None:
            raise ServiceError(404, "not_found", "记录不存在")

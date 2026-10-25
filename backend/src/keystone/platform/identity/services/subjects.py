"""Public subject validation within the caller's identity-locked transaction."""

from sqlalchemy import String, cast, or_, select

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


async def user_labels_in_transaction(session, visible_ids, *, q="", offset=0, limit=100):
    """Resolve names only for caller-authorized subject IDs, including disabled users.

    Caller owns authorization and the transaction. visible_ids is a bounded ID list
    or a scoped SELECT; no identity ORM or credentials cross this public boundary.
    """
    if not 0 <= offset <= 100000 or not 1 <= limit <= 101 or len(q) > 128:
        raise ValueError("Invalid label query")
    rows = await session.execute(
        select(User.id, User.display_name, User.username)
        .where(
            User.id.in_(visible_ids),
            or_(
                User.display_name.icontains(q, autoescape=True),
                User.username.icontains(q, autoescape=True),
                cast(User.id, String).icontains(q, autoescape=True),
            ),
        )
        .order_by(User.display_name, User.id)
        .offset(offset)
        .limit(limit)
    )
    return [{"id": str(row.id), "label": row.display_name or row.username} for row in rows]

"""Flow-owned reference lookup inside a caller's authorized transaction."""

from sqlalchemy import cast, func, or_, select
from sqlalchemy.dialects.postgresql import JSONB, JSONPATH

from keystone.platform.flow.models import FlowRun


async def model_referenced(session, *, entry_id=None, channel_id=None):
    """Caller holds the row lock also used when binding new Runs.

    Terminal Runs still preserve their fixed model selections.
    """
    conditions = []
    for field, value in (("model_entry_id", entry_id), ("channel_id", channel_id)):
        if value is None:
            continue
        variables = cast({"value": str(value)}, JSONB)
        conditions.append(
            func.jsonb_path_exists(
                FlowRun.definition_snapshot,
                cast(f"$.model_bindings.*.selection.{field} ? (@ == $value)", JSONPATH),
                variables,
            )
        )
    return bool(await session.scalar(select(FlowRun.id).where(or_(*conditions)).limit(1)))

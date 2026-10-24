"""Flow-owned reference lookup inside a caller's authorized transaction."""

from sqlalchemy import cast, func, or_, select
from sqlalchemy.dialects.postgresql import JSONB, JSONPATH

from keystone.platform.flow.models import FlowRun


async def model_referenced(session, *, entry_id=None, channel_id=None, key=None):
    """Caller holds the row lock also used when binding new Runs.

    Terminal Runs still preserve selections. Legacy inputs can hold a Selection
    or model_key; unrelated current inputs do not establish model references.
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
        conditions.append(
            ~FlowRun.definition_snapshot.has_key("format_version")
            & func.jsonb_path_exists(
                FlowRun.input,
                cast(f"$.**.{field} ? (@ == $value)", JSONPATH),
                variables,
            )
        )
    if key is not None:
        conditions.append(
            ~FlowRun.definition_snapshot.has_key("format_version")
            & func.jsonb_path_exists(
                FlowRun.input,
                cast("$.**.model_key ? (@ == $value)", JSONPATH),
                cast({"value": key}, JSONB),
            )
        )
    return bool(await session.scalar(select(FlowRun.id).where(or_(*conditions)).limit(1)))

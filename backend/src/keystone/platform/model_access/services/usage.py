"""Trusted cross-domain facts, restricted to a caller-authorized Run and existing transaction."""

from sqlalchemy import select

from keystone.platform.model_access.models import ModelInvocation
from keystone.platform.model_access.schemas.budgets import TEXT_OPERATIONS


async def has_unreported_completed_text_in_transaction(session, run_id):
    return (
        await session.scalar(
            select(ModelInvocation.id)
            .where(
                ModelInvocation.flow_context["flow_run_id"].astext == str(run_id),
                ModelInvocation.operation.in_(TEXT_OPERATIONS),
                ModelInvocation.status != "running",
                ModelInvocation.usage["input_tokens"].astext.is_(None),
            )
            .limit(1)
        )
        is not None
    )

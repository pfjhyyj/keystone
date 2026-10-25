"""Attach newly supported billing to an unchanged original channel, never resend a model."""

from keystone.platform.audit.services import writer as audit
from keystone.platform.model_access.models import Channel, ModelInvocation
from keystone.platform.model_access.services.cost_context import bind_channel
from keystone.platform.model_access.services.cost_policy import initialize_check
from keystone.platform.model_access.services.queries import locked


async def bind_missing_cost_context(session, row, clients, actor, correlation_id=None):
    """Caller authorizes history/manage access and owns the transaction and audit actor."""
    if row.cost_context or row.cost_amount is not None or row.status == "running":
        return False
    # Match the cost writer's Channel -> Invocation lock order, then refresh both rows.
    channel = await locked(session, Channel, row.channel_id)
    row = await locked(session, ModelInvocation, row.id)
    if (
        row.cost_context
        or row.cost_amount is not None
        or row.status == "running"
        or channel.provider != "newapi"
        or channel.updated_at > row.created_at
    ):
        return False
    adapter = clients.billing.adapter(channel.provider, channel.api_base)
    if adapter.cost_query_mode != "logs":
        return False
    identifier = adapter.receipt(row.receipt_ids or {}, None).request_id
    if not identifier:
        return False
    row.cost_context = bind_channel(channel, clients)
    if not row.cost_context:
        return False
    row.upstream_request_id = identifier
    initialize_check(row, row.finished_at)
    await audit.record(
        session,
        "model.cost.bind",
        actor,
        "model.invocation",
        row.id,
        correlation_id=correlation_id,
        details={"source": "unchanged_original_channel"},
    )
    return True

from sqlalchemy import select

from keystone.core.errors import ServiceError
from keystone.core.time_range import history_range
from keystone.platform.model_access.models import ModelInvocation
from keystone.platform.model_access.providers import PROVIDERS


def balance_config(channel):
    adapter = PROVIDERS[channel.provider]
    return (
        channel.provider,
        channel.api_base,
        channel.billing_credential_ref if adapter.uses_billing_key else channel.credential_ref,
        channel.upstream_account_ref,
    )


async def locked(session, model, identifier):
    row = await session.scalar(
        select(model)
        .where(model.id == identifier)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise ServiceError(404, "not_found", "记录不存在")
    return row


def history_scope(auth):
    auth.require("model_invocation:read")
    return (
        []
        if "model_invocation:read_all" in auth.permissions
        else [ModelInvocation.user_id == auth.user.id]
    )


async def history_filters(
    session, auth, since=None, until=None, channel_id=None, status=None, flow_run_id=None
):
    since, until = await history_range(session, since, until)
    filters = history_scope(auth) + [
        ModelInvocation.created_at >= since,
        ModelInvocation.created_at <= until,
    ]
    if channel_id:
        filters.append(ModelInvocation.channel_id == channel_id)
    if status:
        filters.append(ModelInvocation.status == status)
    if flow_run_id:
        filters.append(ModelInvocation.flow_context["flow_run_id"].astext == str(flow_run_id))
    return filters

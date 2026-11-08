from sqlalchemy import case, or_, select

from keystone.core.errors import ServiceError
from keystone.core.time_range import history_range
from keystone.platform.model_access.channels import channel_adapter
from keystone.platform.model_access.models import ModelInvocation


def balance_config(channel):
    adapter = channel_adapter(channel.provider, channel.api_base).billing
    return (
        channel.provider,
        channel.api_base,
        channel.billing_api_key_encrypted
        if adapter.uses_billing_key
        else channel.api_key_encrypted,
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
    session,
    auth,
    since=None,
    until=None,
    channel_id=None,
    status=None,
    flow_run_id=None,
    model_entry_id=None,
    provider_model=None,
    user_id=None,
    node_key=None,
    model_role=None,
    agent_key=None,
    agent_call_id=None,
    operation=None,
    source=None,
    cost_status=None,
    cost_source=None,
    cost_known=None,
    cost_unit=None,
    error_code=None,
    missing=None,
    cost_basis="actual",
    estimate_status=None,
):
    if cost_basis == "estimated" and (cost_status is not None or cost_source is not None):
        raise ServiceError(422, "invalid_cost_basis", "预估口径不支持实扣状态或来源过滤")
    amount, unit, _ = cost_columns(cost_basis)
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
    for column, value in (
        (ModelInvocation.model_entry_id, model_entry_id),
        (ModelInvocation.provider_model, provider_model),
        (ModelInvocation.operation, operation),
        (ModelInvocation.cost_check_status, cost_status),
        (ModelInvocation.cost_source, cost_source),
        (ModelInvocation.estimate_status, estimate_status),
        (ModelInvocation.estimate_currency if cost_basis == "estimated" else unit, cost_unit),
        (ModelInvocation.error_code, error_code),
        (ModelInvocation.flow_context["node_key"].astext, node_key),
        (ModelInvocation.flow_context["model_role"].astext, model_role),
        (ModelInvocation.flow_context["agent_key"].astext, agent_key),
        (
            ModelInvocation.flow_context["agent_call_id"].astext,
            str(agent_call_id) if agent_call_id is not None else None,
        ),
    ):
        if value is not None:
            filters.append(column == value)
    if user_id is not None:
        filters.append(
            ModelInvocation.user_id.is_(None)
            if user_id == "system"
            else ModelInvocation.user_id == user_id
        )
    if source is not None:
        filters.append(invocation_source() == source)
    if cost_known is not None:
        filters.append(amount.is_not(None) if cost_known else amount.is_(None))
    for dimension in missing.split(",") if missing else []:
        columns = {
            "flow_run": ModelInvocation.flow_context["flow_run_id"].astext,
            "node": ModelInvocation.flow_context["node_key"].astext,
            "model_role": ModelInvocation.flow_context["model_role"].astext,
            "agent": ModelInvocation.flow_context["agent_key"].astext,
            "error": ModelInvocation.error_code,
        }
        filters.append(columns[dimension].is_(None))
    return filters


def cost_columns(basis):
    if basis == "estimated":
        return (
            ModelInvocation.estimate_amount,
            case(
                (ModelInvocation.estimate_status == "estimated", ModelInvocation.estimate_currency)
            ),
            ModelInvocation.estimate_status,
        )
    return ModelInvocation.cost_amount, ModelInvocation.cost_unit, ModelInvocation.cost_check_status


def invocation_source():
    return case(
        (
            or_(
                ModelInvocation.flow_context["agent_key"].astext.is_not(None),
                ModelInvocation.flow_context["agent_call_id"].astext.is_not(None),
            ),
            "agent",
        ),
        (ModelInvocation.flow_context["flow_run_id"].astext.is_not(None), "flow"),
        else_="direct",
    )

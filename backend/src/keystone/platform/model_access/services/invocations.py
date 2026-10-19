from decimal import localcontext

from sqlalchemy import func, select

from keystone.core.errors import ServiceError
from keystone.core.time import utcnow
from keystone.platform.identity.access import AccessContext
from keystone.platform.model_access import schemas
from keystone.platform.model_access.models import Channel, ModelInvocation
from keystone.platform.model_access.providers import PROVIDERS
from keystone.platform.model_access.services.queries import history_filters, history_scope, locked


class InvocationService:
    def __init__(self, identity: AccessContext, gateway):
        self.identity, self.gateway = identity, gateway

    async def invocations(self, offset, limit, **filters):
        async with self.identity.access("model_invocation:read") as (session, auth):
            rows = await session.scalars(
                select(ModelInvocation)
                .where(*await history_filters(session, auth, **filters))
                .order_by(ModelInvocation.created_at.desc(), ModelInvocation.id)
                .offset(offset)
                .limit(limit)
            )
            return [schemas.invocation_view(row) for row in rows]

    async def invocation(self, identifier):
        async with self.identity.access("model_invocation:read") as (session, auth):
            row = await session.scalar(
                select(ModelInvocation).where(
                    ModelInvocation.id == identifier, *history_scope(auth)
                )
            )
            if row is None:
                raise ServiceError(404, "not_found", "调用记录不存在或不可访问")
            return schemas.invocation_view(row)

    async def summary(self, **filters):
        async with self.identity.access("model_invocation:read") as (session, auth):
            conditions = await history_filters(session, auth, **filters)
            # One SQL snapshot keeps status counts and cost totals consistent during completion.
            groups = (
                await session.execute(
                    select(
                        ModelInvocation.status,
                        ModelInvocation.channel_id,
                        ModelInvocation.cost_unit,
                        func.sum(ModelInvocation.cost_amount),
                        func.count(),
                    )
                    .where(*conditions)
                    .group_by(
                        ModelInvocation.status,
                        ModelInvocation.channel_id,
                        ModelInvocation.cost_unit,
                    )
                )
            ).all()
            statuses, known, unknown = {}, {}, 0
            for status, channel, unit, total, count in groups:
                statuses[status] = statuses.get(status, 0) + count
                if unit is None:
                    unknown += count
                else:
                    group = known.setdefault(
                        (channel, unit),
                        {"channel_id": channel, "unit": unit, "amount": 0, "attempts": 0},
                    )
                    with localcontext() as context:
                        context.prec = 80  # Preserve Numeric(38,18) across grouped subtotals.
                        group["amount"] += total
                    group["attempts"] += count
            for group in known.values():
                group["amount"] = format(group["amount"], "f")
            return {
                "attempts_by_status": statuses,
                "unknown_cost_attempts": unknown,
                "known_costs": list(known.values()),
            }

    async def refresh_cost(self, identifier):
        async with self.identity.access("model:manage") as (session, auth):
            row = await session.scalar(
                select(ModelInvocation).where(
                    ModelInvocation.id == identifier, *history_scope(auth)
                )
            )
            if row is None:
                raise ServiceError(404, "not_found", "调用记录不存在或不可访问")
            if row.status == "running":
                raise ServiceError(409, "model_call_running", "请等待当前调用结束")
            channel = await session.get(Channel, row.channel_id)
            config = (channel.provider, channel.api_base, channel.credential_ref)
            upstream_id = row.upstream_request_id
        provider, base, reference = config
        adapter, cost, note = PROVIDERS[provider], None, "cost_query_unavailable"
        if adapter.supports_cost_query and upstream_id:
            try:
                path, params = adapter.cost_query(upstream_id)
                payload = await self.gateway.query(
                    provider,
                    base,
                    path,
                    self.gateway.secret(reference),
                    params=params,
                )
                cost = adapter.queried_cost(payload, upstream_id)
                note = None if cost is not None else "cost_not_reported"
            except Exception:
                note = "cost_query_failed"
        async with self.identity.database.transaction() as session:
            row = await locked(session, ModelInvocation, identifier)
            channel = await locked(session, Channel, row.channel_id)
            if (channel.provider, channel.api_base, channel.credential_ref) != config:
                raise ServiceError(
                    409, "model_configuration_changed", "渠道配置已改变，旧费用结果已丢弃"
                )
            if cost is not None:
                row.cost_amount, row.cost_unit, row.cost_source = cost, adapter.cost_unit, "query"
                row.cost_note = None
            elif row.cost_amount is None:
                row.cost_note = note
            row.updated_at = utcnow()
        return await self.invocation(identifier)

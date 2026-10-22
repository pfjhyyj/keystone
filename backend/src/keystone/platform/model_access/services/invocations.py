from decimal import localcontext

from sqlalchemy import func, select

from keystone.core.errors import ServiceError
from keystone.platform.audit.services.writer import Actor
from keystone.platform.identity.services.access import AccessContext
from keystone.platform.model_access import schemas
from keystone.platform.model_access.models import ModelInvocation
from keystone.platform.model_access.services.costs import CostService
from keystone.platform.model_access.services.queries import history_filters, history_scope


class InvocationService:
    def __init__(self, identity: AccessContext, clients):
        self.identity, self.clients = identity, clients

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
                        ModelInvocation.cost_check_status,
                        func.sum(ModelInvocation.cost_amount),
                        func.count(),
                    )
                    .where(*conditions)
                    .group_by(
                        ModelInvocation.status,
                        ModelInvocation.channel_id,
                        ModelInvocation.cost_unit,
                        ModelInvocation.cost_check_status,
                    )
                )
            ).all()
            statuses, known, unknown, checks = {}, {}, 0, {}
            for status, channel, unit, check, total, count in groups:
                checks[check] = checks.get(check, 0) + count
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
                "cost_checks": checks,
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
            scope = row.cost_context.get("scope") if row.cost_context else None
            actor = Actor("user", auth.user.id)
        if scope and row.upstream_request_id:
            await CostService(self.identity.database, self.clients).reconcile(
                scope,
                force_id=identifier,
                actor=actor,
                correlation_id=self.identity.correlation_id,
            )
        return await self.invocation(identifier)

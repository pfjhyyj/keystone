from decimal import localcontext

from sqlalchemy import func, select

from keystone.core.errors import ServiceError
from keystone.platform.audit.services.writer import Actor
from keystone.platform.identity.services.access import AccessContext
from keystone.platform.model_access import schemas
from keystone.platform.model_access.models import ModelInvocation
from keystone.platform.model_access.schemas.diagnostics import (
    ImageSource,
    InvocationDiagnostics,
    diagnostic,
)
from keystone.platform.model_access.schemas.images import ImageManifest
from keystone.platform.model_access.services.costs import CostService
from keystone.platform.model_access.services.queries import history_filters, history_scope


class InvocationService:
    def __init__(self, identity: AccessContext, clients):
        self.identity, self.clients = identity, clients

    async def image_sources_in_transaction(self, session, auth, run_id, sources):
        """Trusted caller holds identity/Run authorization; return confirmed edges only.

        Private result evidence stays inside Model Access. A valid key alone never
        attributes a file: its immutable byte metadata must match the original output.
        """
        if len(sources) > 100:
            raise ValueError("Too many diagnostic sources")
        if not sources or "model_invocation:read" not in auth.permissions:
            return set()
        sources = [ImageSource.model_validate(source) for source in sources]
        rows = await session.execute(
            select(ModelInvocation.id, ModelInvocation.operation, ModelInvocation.result_context)
            .where(
                *history_scope(auth),
                ModelInvocation.id.in_({source.invocation_id for source in sources}),
                ModelInvocation.flow_context["flow_run_id"].astext == str(run_id),
                ModelInvocation.operation.in_(("image.generate", "image.edit")),
            )
            .limit(100)
        )
        manifests = {}
        for row in rows:
            try:
                manifest = ImageManifest.model_validate(row.result_context)
            except ValueError:
                continue
            if manifest.operation != row.operation or any(
                output.artifact_key != f"model.{row.id}.{index}"
                for index, output in enumerate(manifest.outputs)
            ):
                continue
            manifests[row.id] = manifest
        confirmed = set()
        for source in sources:
            manifest = manifests.get(source.invocation_id)
            if manifest is not None and any(
                output.artifact_key == source.artifact_key
                and output.checksum_sha256 == source.checksum_sha256
                and output.size_bytes == source.size_bytes
                and output.content_type == source.content_type
                for output in manifest.outputs
            ):
                confirmed.add((source.file_id, source.invocation_id, source.artifact_key))
        return confirmed

    async def flow_diagnostics_in_transaction(self, session, auth, run_id, offset, limit):
        """Trusted caller authorizes Run and holds the current identity transaction.

        No default history window: an authorized historical Run keeps its facts.
        SQL applies the invocation scope before bounded pagination.
        """
        if "model_invocation:read" not in auth.permissions:
            return InvocationDiagnostics(available=False)
        rows = list(
            await session.execute(
                select(
                    ModelInvocation.id,
                    ModelInvocation.operation,
                    ModelInvocation.status,
                    ModelInvocation.flow_context,
                    ModelInvocation.error_code,
                )
                .where(
                    *history_scope(auth),
                    ModelInvocation.flow_context["flow_run_id"].astext == str(run_id),
                )
                .order_by(ModelInvocation.created_at, ModelInvocation.id)
                .offset(offset)
                .limit(limit + 1)
            )
        )
        truncated = len(rows) > limit
        return InvocationDiagnostics(
            available=True,
            items=[diagnostic(row) for row in rows[:limit]],
            truncated=truncated,
            next_offset=offset + limit if truncated else None,
        )

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

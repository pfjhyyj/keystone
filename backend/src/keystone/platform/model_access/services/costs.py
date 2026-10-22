"""Bounded cost reconciliation: domain pending facts survive DBOS and API restarts."""

import asyncio
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select

from keystone.core.errors import ServiceError
from keystone.core.time import utcnow
from keystone.platform.audit.services import writer as audit
from keystone.platform.model_access.adapters.billing.contracts import BillingQueryError, amount
from keystone.platform.model_access.models import Channel, ModelInvocation
from keystone.platform.model_access.services.cost_policy import (
    MAX_ERRORS,
    WAIT_SECONDS,
    WINDOW,
    backoff,
    expire_check,
    stop_check,
)
from keystone.platform.model_access.services.queries import locked

PAGE_SIZE = 20
PAGES_PER_PASS = 2
BATCH_SIZE = 20
MAX_SCOPES = 20


def scope_filter(scope):
    return ModelInvocation.cost_context["scope"].astext == scope


class CostService:
    def __init__(self, database, clients):
        self.database, self.clients = database, clients

    async def scopes(self):
        now = utcnow()
        async with self.database.transaction() as session:
            overdue = list(
                await session.scalars(
                    select(ModelInvocation)
                    .where(
                        ModelInvocation.cost_check_status == "pending",
                        ModelInvocation.status != "running",
                        ModelInvocation.cost_next_check_at <= now,
                    )
                    .order_by(ModelInvocation.cost_next_check_at, ModelInvocation.id)
                    .limit(100)
                    .with_for_update()
                )
            )
            for row in overdue:
                expire_check(row, now)
            await session.flush()
            scope = ModelInvocation.cost_context["scope"].astext
            return list(
                await session.scalars(
                    select(scope)
                    .where(
                        ModelInvocation.cost_check_status == "pending",
                        ModelInvocation.status != "running",
                        ModelInvocation.cost_next_check_at <= now,
                        scope.is_not(None),
                    )
                    .group_by(scope)
                    .order_by(func.min(ModelInvocation.cost_next_check_at), scope)
                    .limit(MAX_SCOPES)
                )
            )

    async def reconcile(self, scope, *, force_id=None, actor=None, correlation_id=None):
        if not isinstance(scope, str) or len(scope) != 64:
            return
        if force_id is not None and actor is None:
            raise ValueError("Manual recheck requires a trusted actor")
        # Bound coordination connections too, before acquiring a per-scope session lock.
        async with (
            self.clients.coordination.cost_slots,
            self.clients.coordination.cost_scope(UUID(hex=scope[:32])) as still_owned,
        ):
            prepared = await self._prepare(scope, force_id, actor, correlation_id, still_owned)
            if prepared is None:
                return
            config, checkpoint = prepared
            identifier, provider, base, encrypted, context = config
            try:
                secret = self.clients.credentials.decrypt(encrypted, identifier, "inference")
                if self.clients.credentials.scope(context["origin"], secret) != scope:
                    raise BillingQueryError("cost_scope_changed", blocked=True)
                async with asyncio.timeout(25):
                    if context["mode"] == "logs":
                        if not checkpoint["policy_checked"]:
                            await self.clients.billing.validate_policy(provider, context, secret)
                            checkpoint["policy_checked"] = True
                        for _ in range(PAGES_PER_PASS):
                            page = checkpoint["page"]
                            result = await self.clients.billing.page(
                                provider,
                                context,
                                secret,
                                start=checkpoint["start"],
                                end=checkpoint["end"],
                                page=page,
                                page_size=PAGE_SIZE,
                            )
                            receipts, done = result.records, result.complete
                            targets = set(checkpoint["requests"].values())
                            for receipt in receipts:
                                request = receipt["request_id"]
                                if request not in targets:
                                    continue
                                previous = checkpoint["matches"].get(request)
                                checkpoint["matches"][request] = (
                                    {"conflict": True}
                                    if previous is not None and previous != receipt
                                    else receipt
                                )
                            checkpoint["page"] = result.next_page
                            await self._commit(config, checkpoint, still_owned, done=done)
                            if done:
                                return
                    else:
                        for request in checkpoint["requests"].values():
                            fee = await self.clients.billing.lookup(
                                provider, context, secret, request
                            )
                            if fee is not None:
                                checkpoint["matches"][request] = fee.record(request)
                            # Each direct lookup is independently complete; use a one-row batch.
                        await self._commit(config, checkpoint, still_owned, done=True)
            except BillingQueryError as error:
                await self._commit(config, checkpoint, still_owned, error=error)
            except (TimeoutError, ServiceError) as error:
                if isinstance(error, ServiceError) and error.code == "model_configuration_changed":
                    raise
                credential = (
                    isinstance(error, ServiceError) and error.code == "model_credential_unavailable"
                )
                await self._commit(
                    config,
                    checkpoint,
                    still_owned,
                    error=BillingQueryError(
                        "cost_configuration_unavailable" if credential else "cost_transport_failed",
                        blocked=credential,
                    ),
                )

    async def _prepare(self, scope, force_id, actor, correlation_id, lease):
        now = utcnow()
        async with lease.transaction() as session:
            owner = await session.scalar(
                select(Channel)
                .where(Channel.cost_scope == scope)
                .order_by(Channel.created_at, Channel.id)
                .limit(1)
                .with_for_update()
            )
            if owner is None:
                # Rotation never assigns historical invocations to today's scope.
                rows = await session.scalars(
                    select(ModelInvocation)
                    .where(scope_filter(scope), ModelInvocation.cost_check_status == "pending")
                    .limit(BATCH_SIZE)
                    .with_for_update()
                )
                for row in rows:
                    stop_check(row, "blocked", "cost_scope_changed")
                return None
            context = owner.cost_context
            if not context:
                return None
            if owner.api_base != context["api_base"] or owner.provider != context["provider"]:
                owner.cost_block_reason = "cost_scope_changed"
            if force_id is not None:
                row = await locked(session, ModelInvocation, force_id)
                if not row.cost_context or row.cost_context != context:
                    raise ServiceError(409, "model_configuration_changed", "原查费范围已改变")
                if row.status == "running":
                    raise ServiceError(409, "model_call_running", "请等待当前调用结束")
                previous = row.cost_check_status
                row.cost_check_rounds = row.cost_check_errors = 0
                row.cost_deadline_at = now + WINDOW
                row.cost_check_status, row.cost_check_reason, row.cost_next_check_at = (
                    "pending",
                    None,
                    now,
                )
                owner.cost_block_reason = owner.cost_next_allowed_at = None
                # Restart an explicitly requested window rather than hijacking old pagination.
                owner.cost_checkpoint = None
                await audit.record(
                    session,
                    "model.cost.recheck",
                    actor,
                    "model.invocation",
                    row.id,
                    correlation_id=correlation_id,
                    details={"previous_status": previous},
                )
                await session.flush()
            checkpoint = owner.cost_checkpoint
            if checkpoint and checkpoint.get("context") != context:
                owner.cost_checkpoint = checkpoint = None
            if checkpoint:
                rows = list(
                    await session.scalars(
                        select(ModelInvocation)
                        .where(ModelInvocation.id.in_([UUID(v) for v in checkpoint["ids"]]))
                        .with_for_update()
                    )
                )
                rows = [
                    row
                    for row in rows
                    if row.cost_check_status == "pending" and not expire_check(row, now)
                ]
                if not rows:
                    owner.cost_checkpoint = None
                    return None
            else:
                rows = list(
                    await session.scalars(
                        select(ModelInvocation)
                        .where(
                            scope_filter(scope),
                            ModelInvocation.status != "running",
                            ModelInvocation.cost_check_status == "pending",
                            ModelInvocation.cost_next_check_at <= now,
                            *([ModelInvocation.id == force_id] if force_id else []),
                        )
                        .order_by(ModelInvocation.cost_next_check_at, ModelInvocation.id)
                        .limit(1 if context["mode"] == "request" else BATCH_SIZE)
                        .with_for_update()
                    )
                )
                rows = [row for row in rows if not expire_check(row, now)]
            if not rows:
                return None
            if owner.cost_block_reason:
                for row in rows:
                    stop_check(row, "blocked", owner.cost_block_reason)
                return None
            if owner.cost_next_allowed_at and owner.cost_next_allowed_at > now:
                for row in rows:
                    row.cost_next_check_at = owner.cost_next_allowed_at
                return None
            valid = []
            for row in rows:
                if row.cost_context != context:
                    stop_check(row, "blocked", "cost_scope_changed")
                elif not row.upstream_request_id:
                    stop_check(row, "unavailable", "cost_request_id_missing")
                else:
                    valid.append(row)
            if not valid:
                owner.cost_checkpoint = None
                return None
            if checkpoint is None:
                checkpoint = {
                    "round": uuid4().hex,
                    "context": dict(context),
                    "ids": [str(row.id) for row in valid],
                    "requests": {str(row.id): row.upstream_request_id for row in valid},
                    "start": int(min(row.created_at for row in valid).timestamp()) - 120,
                    "end": int(now.timestamp()),
                    "page": 1,
                    "matches": {},
                    "policy_checked": False,
                }
                owner.cost_checkpoint = checkpoint
            config = (
                owner.id,
                owner.provider,
                owner.api_base,
                owner.api_key_encrypted,
                dict(context),
            )
            return config, dict(checkpoint, matches=dict(checkpoint["matches"]))

    async def _commit(self, config, checkpoint, still_owned, *, done=False, error=None):
        identifier, provider, base, encrypted, context = config
        now = utcnow()
        async with still_owned.transaction() as session:
            owner = await locked(session, Channel, identifier)
            if (owner.provider, owner.api_base, owner.api_key_encrypted, owner.cost_context) != (
                provider,
                base,
                encrypted,
                context,
            ):
                raise ServiceError(
                    409, "model_configuration_changed", "渠道配置已改变，旧费用结果已丢弃"
                )
            if (
                not owner.cost_checkpoint
                or owner.cost_checkpoint["round"] != checkpoint["round"]
                or owner.cost_checkpoint.get("revision", 0) != checkpoint.get("revision", 0)
            ):
                raise ServiceError(409, "model_configuration_changed", "查费轮次已改变")
            rows = list(
                await session.scalars(
                    select(ModelInvocation)
                    .where(ModelInvocation.id.in_([UUID(v) for v in checkpoint["ids"]]))
                    .order_by(ModelInvocation.id)
                    .with_for_update()
                )
            )
            channels = {
                row.id: row
                for row in await session.scalars(
                    select(Channel)
                    .where(Channel.id.in_({row.channel_id for row in rows}))
                    .order_by(Channel.id)
                    .with_for_update()
                )
            }
            for row in rows:
                if row.cost_check_status != "pending" or expire_check(row, now):
                    continue
                if (
                    row.cost_context != context
                    or channels[row.channel_id].cost_context != context
                    or checkpoint["requests"].get(str(row.id)) != row.upstream_request_id
                ):
                    stop_check(row, "blocked", "cost_scope_changed")
                    continue
                if error is not None:
                    row.cost_checked_at = now
                    row.cost_check_reason = error.reason
                    if row.cost_amount is None:
                        row.cost_note = error.reason
                    if error.blocked:
                        stop_check(row, "blocked", error.reason)
                    else:
                        row.cost_check_errors += 1
                        if row.cost_check_errors > MAX_ERRORS:
                            stop_check(row, "exhausted", "cost_retries_exhausted")
                        else:
                            delay = max(backoff(row.cost_check_errors), error.retry_after or 0)
                            row.cost_next_check_at = now + timedelta(seconds=delay)
                            owner.cost_next_allowed_at = max(
                                owner.cost_next_allowed_at or now, row.cost_next_check_at
                            )
                    continue
                if not done:
                    row.cost_next_check_at = now + timedelta(seconds=5)
                    continue
                row.cost_check_rounds += 1
                row.cost_checked_at = now
                receipt = checkpoint["matches"].get(row.upstream_request_id)
                duplicate = await session.scalar(
                    select(ModelInvocation.id)
                    .where(
                        scope_filter(context["scope"]),
                        ModelInvocation.upstream_request_id == row.upstream_request_id,
                        ModelInvocation.id != row.id,
                    )
                    .limit(1)
                )
                if duplicate or (
                    receipt
                    and (
                        receipt.get("conflict")
                        or receipt.get("model", row.provider_model) != row.provider_model
                    )
                ):
                    stop_check(row, "blocked", "cost_receipt_conflict")
                elif receipt:
                    cost = amount(receipt["amount"])
                    if cost is None or receipt["unit"] != context["unit"]:
                        stop_check(row, "blocked", "cost_invalid_response")
                        continue
                    if (
                        row.cost_source == "query"
                        and row.cost_amount is not None
                        and (row.cost_amount != cost or row.cost_unit != receipt["unit"])
                    ):
                        stop_check(row, "blocked", "cost_receipt_conflict")
                        continue
                    previous = (
                        {
                            "amount": format(row.cost_amount, "f"),
                            "unit": row.cost_unit,
                            "source": row.cost_source,
                        }
                        if row.cost_amount is not None and row.cost_source != "query"
                        else (row.cost_evidence or {}).get("previous")
                    )
                    row.cost_evidence = {
                        **receipt,
                        "checked_at": now.isoformat(),
                        "policy": context["policy"],
                        **({"previous": previous} if previous else {}),
                    }
                    row.cost_amount, row.cost_unit, row.cost_source = cost, receipt["unit"], "query"
                    row.cost_note = None
                    stop_check(row, "confirmed")
                else:
                    anchor = (
                        row.cost_deadline_at - WINDOW if row.cost_deadline_at else row.finished_at
                    )
                    elapsed = (now - anchor).total_seconds()
                    delay = next((v for v in WAIT_SECONDS if v > elapsed), WAIT_SECONDS[-1])
                    row.cost_next_check_at = min(
                        anchor + timedelta(seconds=delay),
                        row.cost_deadline_at or row.finished_at + WINDOW,
                    )
                    if row.cost_next_check_at <= now:
                        row.cost_next_check_at = now + timedelta(seconds=5)
                    row.cost_check_reason = "cost_not_reported"
                    if row.cost_amount is None:
                        row.cost_note = "cost_not_reported"
                row.updated_at = now
            if error is not None and error.blocked:
                owner.cost_block_reason = error.reason
            owner.cost_checkpoint = (
                None
                if done or (error is not None and error.blocked)
                else {**checkpoint, "revision": checkpoint.get("revision", 0) + 1}
            )
        checkpoint["revision"] = checkpoint.get("revision", 0) + 1

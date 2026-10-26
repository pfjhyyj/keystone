"""Run-owned cumulative reservations, separate from execution checkpoints and billing."""

import hashlib
import json
import re
from typing import get_args

from keystone.core.errors import ServiceError
from keystone.core.time import utcnow
from keystone.platform.flow.models import FlowBudgetClaim, FlowRun, FlowRunBudget
from keystone.platform.flow.schemas.budgets import BudgetCounters, ClaimKind, FlowLimits
from keystone.platform.flow.schemas.snapshots import read_snapshot
from keystone.platform.model_access.schemas.budgets import TOKEN_OPERATIONS, ModelRequestBudget
from keystone.platform.model_access.services.usage import (
    has_unreported_completed_text_in_transaction,
)


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def claim_key(value):
    if not isinstance(value, str) or not 1 <= len(value.encode()) <= 512:
        raise ValueError("Invalid stable budget key")
    return hashlib.sha256(value.encode()).hexdigest()


async def budget_in_transaction(session, run_id):
    row = await session.get(FlowRunBudget, run_id, with_for_update=True)
    if row is None:
        run = await session.get(FlowRun, run_id)
        if run is not None and read_snapshot(run.definition_snapshot).get("limits") is None:
            return None
        raise ServiceError(503, "flow_budget_unavailable", "运行预算状态不可用")
    try:
        FlowLimits.model_validate(row.limits)
        BudgetCounters.model_validate(row.counters)
    except ValueError:
        raise ServiceError(503, "flow_budget_unavailable", "运行预算状态不可用") from None
    return row


async def reserve_in_transaction(session, run_id, *, kind, key, amounts, request_digest):
    """Caller owns authorization/transaction; lock Budget after dispatch, before File."""
    if kind not in get_args(ClaimKind) or not re.fullmatch(r"[0-9a-f]{64}", request_digest):
        raise ValueError("Invalid budget claim")
    key = claim_key(key)
    amounts = BudgetCounters.model_validate(amounts).model_dump()
    row = await budget_in_transaction(session, run_id)
    if row is None:
        return False
    old = await session.get(FlowBudgetClaim, (run_id, kind, key))
    if old is not None:
        if old.amounts != amounts or old.request_digest != request_digest:
            raise ServiceError(409, "flow_budget_conflict", "同一预算标识对应不同操作")
        return False
    counters = dict(row.counters)
    for name, amount in amounts.items():
        counters[name] += amount
        if amount and name in row.limits and counters[name] > row.limits[name]:
            raise ServiceError(409, "flow_budget_exceeded", "运行累计预算已耗尽")
    row.counters = BudgetCounters.model_validate(counters).model_dump()
    row.updated_at = utcnow()
    session.add(
        FlowBudgetClaim(
            run_id=run_id,
            kind=kind,
            key=key,
            amounts=amounts,
            request_digest=request_digest,
        )
    )
    await session.flush()
    return True


async def initialize_in_transaction(session, run_id, limits, files):
    limits = FlowLimits.model_validate(limits).model_dump()
    session.add(FlowRunBudget(run_id=run_id, limits=limits, counters=BudgetCounters().model_dump()))
    await session.flush()
    for file in files:
        await reserve_in_transaction(
            session,
            run_id,
            kind="file",
            key=f"input:{file['id']}",
            amounts={"file_count": 1, "file_bytes": file["size_bytes"]},
            request_digest=digest(file),
        )


class FlowBudgetService:
    def __init__(self, identity):
        self.identity, self.run_id = identity, identity.run_id

    async def limits(self):
        async with self.identity.access() as (session, _):
            row = await budget_in_transaction(session, self.run_id)
            return FlowLimits.model_validate(row.limits) if row is not None else None

    async def consume(self, kind, key, request_digest):
        if kind not in {"tool", "iteration"}:
            raise ValueError("Only explicit tool/iteration consumption is public")
        async with self.identity.access() as (session, _):
            await reserve_in_transaction(
                session,
                self.run_id,
                kind=kind,
                key=key,
                amounts={"tool_calls" if kind == "tool" else "iterations": 1},
                request_digest=request_digest,
            )

    async def reserve_artifact(self, key, data):
        metadata = {
            "name": data.name,
            "content_type": data.content_type,
            "size_bytes": data.expected_size,
            "checksum_sha256": data.checksum_sha256,
        }
        async with self.identity.access("file:create") as (session, _):
            await reserve_in_transaction(
                session,
                self.run_id,
                kind="file",
                key="artifact:" + key,
                amounts={"file_count": 1, "file_bytes": data.expected_size},
                request_digest=digest(metadata),
            )

    async def reserve_model(self, session, identifier, request):
        request = ModelRequestBudget.model_validate(request.model_dump())
        row = await budget_in_transaction(session, self.run_id)
        if row is None:
            return
        uses_tokens = request.operation in TOKEN_OPERATIONS
        if uses_tokens and (
            row.counters["input_tokens"] >= row.limits["input_tokens"]
            or await has_unreported_completed_text_in_transaction(session, self.run_id)
        ):
            raise ServiceError(409, "flow_budget_exceeded", "输入 token 预算已耗尽或存在未知用量")
        await reserve_in_transaction(
            session,
            self.run_id,
            kind="model",
            key=str(identifier),
            amounts={
                "model_requests": 1,
                "output_tokens": request.max_output_tokens,
                "unreported_text_requests": int(uses_tokens),
            },
            request_digest=digest(request.model_dump()),
        )

    async def settle_model(self, session, identifier, usage):
        """Known attempt only, after send; no fresh authority or new I/O is granted here."""
        row = await budget_in_transaction(session, self.run_id)
        if row is None:
            return
        key = claim_key(str(identifier))
        original = await session.get(FlowBudgetClaim, (self.run_id, "model", key))
        if original is None:
            raise ServiceError(503, "flow_budget_unavailable", "原模型预算预留不可用")
        if not original.amounts["unreported_text_requests"]:
            return
        input_tokens = usage.get("input_tokens") if isinstance(usage, dict) else None
        if type(input_tokens) is not int or not 0 <= input_tokens < 2**63:
            input_tokens = None
        request_digest = digest([input_tokens])
        old = await session.get(FlowBudgetClaim, (self.run_id, "tokens", key))
        if old is not None:
            if old.request_digest != request_digest:
                raise ServiceError(409, "flow_budget_conflict", "原调用用量证据出现冲突")
            return
        counters = dict(row.counters)
        if input_tokens is not None:
            counters["input_tokens"] += input_tokens
            counters["unreported_text_requests"] -= 1
        # Observed input may exceed the limit. Preserve the fact and stop future text sends.
        row.counters = BudgetCounters.model_validate(counters).model_dump()
        row.updated_at = utcnow()
        session.add(
            FlowBudgetClaim(
                run_id=self.run_id,
                kind="tokens",
                key=key,
                amounts=BudgetCounters(input_tokens=input_tokens or 0).model_dump(),
                request_digest=request_digest,
            )
        )
        await session.flush()

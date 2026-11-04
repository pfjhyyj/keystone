"""Explicit Run diagnostic response; provider evidence and checkpoints stay private."""

import re
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field

from keystone.core.schemas import Input
from keystone.platform.flow.schemas.budgets import BudgetCounters, FlowLimits
from keystone.platform.model_access.schemas.diagnostics import InvocationDiagnostics

DiagnosticSection = Literal["all", "budget", "models", "files"]
DecimalCount = Annotated[str, Field(pattern=r"^(0|[1-9][0-9]{0,38})$")]


class BudgetAmounts(Input):
    model_requests: DecimalCount
    tool_calls: DecimalCount
    iterations: DecimalCount
    file_count: DecimalCount
    file_bytes: DecimalCount
    input_tokens: DecimalCount
    output_tokens: DecimalCount


class BudgetLimitValues(BudgetAmounts):
    concurrent_operations: DecimalCount
    temporary_bytes: DecimalCount


class BudgetCounterValues(BudgetAmounts):
    unreported_text_requests: DecimalCount


class BudgetDiagnostic(Input):
    recorded: bool
    limits: BudgetLimitValues | None = None
    counters: BudgetCounterValues | None = None

    @classmethod
    def from_values(cls, limits: FlowLimits, counters: BudgetCounters):
        # Decimal strings preserve every valid integer in browser JSON clients.
        return cls(
            recorded=True,
            limits=BudgetLimitValues(
                **{key: str(value) for key, value in limits.model_dump().items()}
            ),
            counters=BudgetCounterValues(
                **{key: str(value) for key, value in counters.model_dump().items()}
            ),
        )


class FlowFileDiagnostic(Input):
    file_id: UUID
    slot: str = Field(min_length=1, max_length=100)
    invocation_id: UUID | None = None


class FlowFileDiagnostics(Input):
    items: list[FlowFileDiagnostic] = []
    truncated: bool = False
    next_offset: int | None = None


def source_invocation(slot):
    match = re.fullmatch(r"artifact\.model\.([0-9a-f-]{36})\.[0-3]", slot)
    if match:
        try:
            return UUID(match[1])
        except ValueError:
            pass
    return None


class Diagnostics(Input):
    run_id: UUID
    budget: BudgetDiagnostic | None = None
    models: InvocationDiagnostics | None = None
    files: FlowFileDiagnostics | None = None

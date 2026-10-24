"""Immutable trusted limits and bounded cumulative facts; no persistence/execution imports."""

from typing import Annotated, Literal

from pydantic import ConfigDict, Field

from keystone.core.schemas import Input

Count = Annotated[int, Field(strict=True, ge=0, le=2**127 - 1)]
ClaimKind = Literal["model", "tokens", "tool", "iteration", "file"]


class FlowLimits(Input):
    model_config = ConfigDict(frozen=True)

    model_requests: int = Field(default=64, strict=True, ge=0, le=4096)
    tool_calls: int = Field(default=256, strict=True, ge=0, le=16384)
    iterations: int = Field(default=256, strict=True, ge=0, le=16384)
    concurrent_operations: int = Field(default=4, strict=True, ge=1, le=16)
    file_count: int = Field(default=100, strict=True, ge=0, le=10000)
    file_bytes: int = Field(default=256 * 1024**2, strict=True, ge=0, le=32 * 1024**4)
    temporary_bytes: int = Field(default=64 * 1024**2, strict=True, ge=0, le=32 * 1024**4)
    input_tokens: int = Field(default=1000000, strict=True, ge=0, le=2**63 - 1)
    output_tokens: int = Field(default=65536, strict=True, ge=0, le=2**63 - 1)


class BudgetCounters(Input):
    model_requests: Count = 0
    tool_calls: Count = 0
    iterations: Count = 0
    file_count: Count = 0
    file_bytes: Count = 0
    input_tokens: Count = 0
    output_tokens: Count = 0
    unreported_text_requests: Count = 0

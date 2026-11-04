"""Trusted budget limits remain immutable, bounded, lightweight and historically explicit."""

from dataclasses import replace

import pytest
from pydantic import ValidationError
from support.flow_packages import snapshot_for

from keystone.examples.document_review.contracts import definition
from keystone.platform.flow.schemas.budgets import BudgetCounters, FlowLimits
from keystone.platform.flow.schemas.snapshots import (
    matches_definition,
    public_snapshot,
    read_snapshot,
)
from keystone.platform.model_access.schemas.budgets import ModelRequestBudget


def test_limits_are_strict_immutable_and_not_a_public_request_override():
    limits = FlowLimits()
    for changes in (
        {"model_requests": True},
        {"tool_calls": -1},
        {"iterations": "5"},
        {"concurrent_operations": 0},
        {"file_count": 10001},
        {"temporary_bytes": 32 * 1024**4 + 1},
        {"future_field": 1},
    ):
        with pytest.raises(ValidationError):
            FlowLimits(**changes)
    with pytest.raises(ValidationError):
        limits.model_requests = 1
    assert FlowLimits(model_requests=0, file_bytes=0, input_tokens=0).model_requests == 0
    with pytest.raises(ValidationError):
        BudgetCounters(input_tokens=-1)
    with pytest.raises(ValidationError):
        BudgetCounters(input_tokens=True)


def test_budget_snapshots_preserve_declared_limits():
    current = replace(definition, limits=FlowLimits(model_requests=2))
    value = snapshot_for(current)
    assert value["limits"]["model_requests"] == 2
    assert matches_definition(read_snapshot(value), current)
    assert public_snapshot(value)["limits"] == value["limits"]


def test_request_allowances_match_actual_text_or_image_operation():
    assert (
        ModelRequestBudget(operation="text.generate", max_output_tokens=1024).max_output_tokens
        == 1024
    )
    assert (
        ModelRequestBudget(operation="image.generate", max_output_tokens=0).max_output_tokens == 0
    )
    for operation, tokens in (("image.edit", 1), ("text.structured", 0), ("text.generate", True)):
        with pytest.raises(ValidationError):
            ModelRequestBudget(operation=operation, max_output_tokens=tokens)

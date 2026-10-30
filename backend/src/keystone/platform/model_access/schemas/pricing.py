"""Versioned, bounded pricing inputs. No executable expressions or raw requests."""

import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal

from pydantic import Field, StrictInt, StrictStr, field_validator, model_validator

from keystone.core.schemas import Input

from .operations import Delivery, Operation


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def version_one(value):
    if type(value) is not int or value != 1:
        raise ValueError("Unknown pricing format")
    return value


def bounded(value, maximum):
    try:
        size = len(json.dumps(value, ensure_ascii=False).encode())
    except (TypeError, RecursionError):
        raise ValueError("Invalid pricing document") from None
    if size > maximum:
        raise ValueError("Pricing document exceeds its size limit")
    return value


def decimal_string(value):
    if (
        not isinstance(value, str)
        or len(value) > 48
        or not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,18})?", value)
    ):
        raise ValueError("Use a nonnegative decimal string")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise ValueError("Use a decimal string") from None
    if (
        not number.is_finite()
        or number < 0
        or number >= Decimal("1e20")
        or number.as_tuple().exponent < -18
    ):
        raise ValueError("Decimal is outside pricing limits")
    return value


Identifier = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,47}$")]
Scalar = StrictInt | Annotated[StrictStr, Field(max_length=128)]


class Predicate(Input):
    field: str | None = Field(None, max_length=80)
    op: Literal["eq", "in", "lt", "lte", "gt", "gte"] | None = None
    value: Scalar | list[Scalar] | None = None
    all: list["Predicate"] | None = Field(None, min_length=1, max_length=8)
    any: list["Predicate"] | None = Field(None, min_length=1, max_length=8)

    @model_validator(mode="after")
    def one_expression(self):
        comparison = self.field is not None and self.op is not None and self.value is not None
        if sum((comparison, self.all is not None, self.any is not None)) != 1:
            raise ValueError("A predicate needs exactly one expression")
        if not comparison and any(v is not None for v in (self.field, self.op, self.value)):
            raise ValueError("Mixed predicate expressions")
        if comparison and (isinstance(self.value, list) != (self.op == "in")):
            raise ValueError("Only in accepts a list")
        if isinstance(self.value, list) and not 1 <= len(self.value) <= 32:
            raise ValueError("Predicate list is outside limits")
        return self


class PriceItem(Input):
    id: Identifier
    quantity: str = Field(max_length=80)
    rate: str
    per: int = Field(strict=True, ge=1, le=10**12)

    _rate = field_validator("rate", mode="before")(decimal_string)


class Price(Input):
    kind: Literal["price"]
    id: Identifier
    input_partition: Literal[
        "none", "aggregate", "cache", "cache_read", "modality", "modality_cache"
    ]
    output_partition: Literal["none", "aggregate", "reasoning", "modality"]
    items: list[PriceItem] = Field(min_length=1, max_length=16)
    factor: str

    @field_validator("factor", mode="before")
    @classmethod
    def factor_limit(cls, value):
        decimal_string(value)
        if Decimal(value) > 10**6:
            raise ValueError("Factor exceeds limit")
        return value


class Branch(Input):
    kind: Literal["branch"]
    when: Predicate
    then: "Node"
    otherwise: "Node" = Field(alias="else")


Node = Annotated[Price | Branch, Field(discriminator="kind")]
Branch.model_rebuild()


class TimeBasis(Input):
    source: Literal["observed_response", "upstream_completed"]
    timezone: str = Field(min_length=1, max_length=80)
    calendar: Literal["all_days", "weekdays"] = "all_days"


class PriceRule(Input):
    format: Literal[1]
    currency: Literal["CNY", "USD", "EUR", "GBP", "JPY", "HKD", "SGD", "AUD", "CAD", "KRW"]
    root: Node
    time_basis: TimeBasis | None = None

    @model_validator(mode="after")
    def normalized_limits(self):
        version_one(self.format)
        bounded(self.model_dump(mode="json", by_alias=True), 16384)
        return self

    @model_validator(mode="before")
    @classmethod
    def rule_limits(cls, value):
        if isinstance(value, dict):
            if type(value.get("format")) is not int or value["format"] != 1:
                raise ValueError("Unknown pricing format")
            bounded(value, 16384)
            stack, count = [(value.get("root"), 1)], 0
            while stack:
                node, depth = stack.pop()
                count += 1
                if count > 64 or depth > 8:
                    raise ValueError("Pricing tree exceeds limits")
                if isinstance(node, dict):
                    for key in ("then", "else", "when"):
                        if key in node:
                            stack.append((node[key], depth + 1))
                    for key in ("all", "any"):
                        if isinstance(node.get(key), list):
                            stack.extend((child, depth + 1) for child in node[key])
        return value


class PriceSource(Input):
    label: str = Field(min_length=1, max_length=160)
    url: str | None = Field(None, max_length=512, pattern=r"^https://[^\s]+$")
    version: str = Field(min_length=1, max_length=80)
    checked_at: str | None = Field(None, max_length=40)


class OperationPricing(Input):
    operation: Operation
    delivery: Delivery = "sync"
    rule: PriceRule
    source: PriceSource


class PricingConfig(Input):
    mode: Literal["inherit", "override", "disabled"] = "inherit"
    rules: list[OperationPricing] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def unique_targets(self):
        targets = [(r.operation, r.delivery) for r in self.rules]
        if len(targets) != len(set(targets)) or (self.mode != "override" and self.rules):
            raise ValueError("Duplicate targets or rules outside override mode")
        if self.mode == "override" and not self.rules:
            raise ValueError("Override requires an explicit operation rule")
        bounded(self.model_dump(mode="json", by_alias=True), 196608)
        return self


class PricingEdit(Input):
    expected_revision: int = Field(strict=True, ge=0)
    config: PricingConfig


class Measurement(Input):
    value: int = Field(strict=True, ge=0, lt=2**63)
    unit: Literal["token", "image", "request"]
    source: Literal["upstream_usage", "upstream_result", "derived", "protocol", "manual"]
    complete: bool = Field(strict=True, default=True)


class PricingFacts(Input):
    format: Literal[1] = 1
    _version = field_validator("format", mode="before")(version_one)
    profile: str = Field(max_length=64)
    values: dict[str, Measurement] = Field(default_factory=dict, max_length=32)
    context: dict[str, Scalar] = Field(default_factory=dict, max_length=32)
    error: Literal["meter_conflict"] | None = None

    @model_validator(mode="after")
    def size_limit(self):
        version_one(self.format)
        bounded(self.model_dump(mode="json"), 8192)
        return self


class PricingSnapshot(Input):
    format: Literal[1] = 1
    _version = field_validator("format", mode="before")(version_one)
    profile: str = Field(max_length=64)
    binding: dict[str, str] = Field(max_length=6)
    revision: int = Field(strict=True, ge=0)
    context: dict[str, Scalar] = Field(default_factory=dict, max_length=32)
    rule: PriceRule | None = None
    source: PriceSource | None = None
    reason: (
        Literal["price_missing", "pricing_disabled", "pricing_binding_changed", "pricing_invalid"]
        | None
    ) = None

    @model_validator(mode="after")
    def size_limit(self):
        version_one(self.format)
        if self.rule is not None and self.source is None:
            raise ValueError("A resolved price requires its source")
        if set(self.binding) != {
            "channel_id",
            "provider",
            "api_base",
            "provider_model",
            "operation",
            "delivery",
        }:
            raise ValueError("Pricing snapshot requires a complete target binding")
        if (self.rule is None) != (self.reason is not None):
            raise ValueError("Pricing snapshot requires a rule or an explicit resolution reason")
        bounded(self.model_dump(mode="json", by_alias=True), 24576)
        return self


class EstimateResult(Input):
    status: Literal["pending", "estimated", "partial", "unavailable", "invalid"]
    amount: str | None = None
    currency: str | None = None
    reason: str | None = Field(None, max_length=64)
    detail: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def valid_result(self):
        if self.status == "estimated":
            decimal_string(self.amount)
            if self.currency is None:
                raise ValueError("An estimate requires its currency")
        elif self.amount is not None:
            raise ValueError("Only complete estimates have a total")
        bounded(self.detail, 16384)
        return self


class PricingPreview(Input):
    config: PricingConfig
    operation: Operation
    delivery: Delivery = "sync"
    context: dict[str, Scalar] = Field(default_factory=dict, max_length=32)
    facts: PricingFacts


class MetricProfile(Input):
    id: str
    quantities: dict[str, Literal["token", "image", "request"]]
    context_fields: dict[str, Literal["string", "integer"]]
    input_partitions: dict[str, list[str]]
    output_partitions: dict[str, list[str]]
    structural_zero: list[str] = Field(default_factory=list)
    context_enums: dict[str, list[Scalar]] = Field(default_factory=dict)

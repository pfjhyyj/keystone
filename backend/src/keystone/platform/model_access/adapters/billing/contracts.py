"""Internal billing facts. No ORM objects, secrets, SDK handles or scheduler context."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import NotRequired, TypedDict


class BillingQueryError(Exception):
    def __init__(self, reason, *, blocked=False, retry_after=None, missing=False):
        super().__init__(reason)
        self.reason, self.blocked = reason, blocked
        self.retry_after, self.missing = retry_after, missing


@dataclass(frozen=True)
class Fee:
    amount: Decimal
    unit: str

    def record(self, identifier):
        return {"request_id": identifier, "amount": format(self.amount, "f"), "unit": self.unit}


@dataclass
class Receipt:
    request_id: str | None = None
    fee: Fee | None = None


class CostRecord(TypedDict):
    request_id: str
    amount: str
    unit: str
    quota: NotRequired[int]
    quota_per_unit: NotRequired[int]
    model: NotRequired[str]


@dataclass(frozen=True)
class LogPage:
    records: list[CostRecord]
    next_page: int
    complete: bool


@dataclass(frozen=True)
class Binding:
    origin: str
    mode: str
    unit: str
    quota_per_unit: int | None
    policy: str

    def snapshot(self, scope, provider, api_base):
        return {
            "scope": scope,
            "provider": provider,
            "origin": self.origin,
            "api_base": api_base,
            "mode": self.mode,
            "unit": self.unit,
            "quota_per_unit": self.quota_per_unit,
            "policy": self.policy,
        }


class ResponseBilling:
    cost_query_mode = "none"
    cost_unit = None
    uses_billing_key = False
    enabled = False
    balance_path = None

    def binding(self, api_base):
        return None

    def receipt(self, identifiers, reported_cost):
        identifier = identifiers.get("body_id") or identifiers.get("x-request-id")
        return Receipt(identifier)

    def check_target(self, base, path):
        from keystone.core.errors import ServiceError

        if not self.enabled:
            raise ServiceError(503, "model_provider_unavailable", "当前服务商尚未开放账务查询")
        if base != self.api_base or path not in self.paths():
            raise ServiceError(409, "model_target_not_allowed", "账务查询目标不受支持")

    def paths(self):
        return {self.balance_path} if self.balance_path else set()


def amount(value, *, signed=False):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
        if (
            not result.is_finite()
            or result.copy_abs() >= Decimal("1e20")
            or result.as_tuple().exponent < -18
            or (not signed and result < 0)
        ):
            return None
        return result
    except (InvalidOperation, ValueError, TypeError):
        return None


def balance_item(kind, value, unit):
    parsed = amount(value, signed=True)
    if parsed is None:
        raise ValueError("Invalid amount")
    return {"kind": kind, "amount": format(parsed, "f"), "unit": unit}

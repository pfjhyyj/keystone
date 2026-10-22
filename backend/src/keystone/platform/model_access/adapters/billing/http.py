"""Billing HTTP classification; endpoint-specific missing semantics are explicit."""

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from keystone.core.errors import ServiceError
from keystone.platform.model_access.adapters.billing.contracts import BillingQueryError
from keystone.platform.model_access.adapters.http import ResponseTooLarge


def retry_delay(value):
    if not value:
        return None
    try:
        delay = (
            int(value)
            if value.isdigit()
            else int((parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds())
        )
        return max(1, min(delay, 86400))
    except (ValueError, TypeError, OverflowError):
        return None


async def cost_get(
    http, adapter, base, path, *, headers=None, params=None, limit=262144, missing=False
):
    def check(response):
        status = response.status_code
        if status in {401, 403}:
            raise BillingQueryError("cost_auth_required", blocked=True)
        if status == 429:
            raise BillingQueryError(
                "cost_rate_limited", retry_after=retry_delay(response.headers.get("retry-after"))
            )
        if status >= 500:
            raise BillingQueryError("cost_upstream_unavailable")
        if status == 404 and missing:
            raise BillingQueryError("cost_not_reported", missing=True)
        if status != 200:
            raise BillingQueryError("cost_query_rejected", blocked=True)

    try:
        adapter.check_target(base, path)
        payload = await http.get_json(
            adapter.provider,
            base,
            path,
            headers=headers,
            params=params,
            limit=limit,
            check_status=check,
        )
        if not isinstance(payload, dict):
            raise ValueError("Invalid envelope")
        if payload.get("success") is False:
            raise BillingQueryError("cost_query_rejected", blocked=True)
        return payload
    except BillingQueryError:
        raise
    except ResponseTooLarge:
        raise BillingQueryError("cost_response_too_large", blocked=True) from None
    except (ValueError, KeyError, TypeError, RecursionError):
        raise BillingQueryError("cost_invalid_response", blocked=True) from None
    except ServiceError:
        raise BillingQueryError("cost_configuration_unavailable", blocked=True) from None
    except Exception:
        raise BillingQueryError("cost_transport_failed") from None


async def balance_get(http, adapter, base, secret):
    adapter.check_target(base, adapter.balance_path)

    def check(response):
        if response.status_code != 200:
            raise ServiceError(502, "model_query_failed", "渠道查询失败")

    try:
        payload = await http.get_json(
            adapter.provider,
            base,
            adapter.balance_path,
            headers={"Authorization": "Bearer " + secret},
            check_status=check,
        )
        return adapter.balance(payload)
    except ResponseTooLarge:
        raise ServiceError(502, "model_query_failed", "渠道查询响应超过上限") from None

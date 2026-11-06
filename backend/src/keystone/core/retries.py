"""Finite retry policy for explicitly idempotent operations; no execution state."""

import asyncio
import math
from asyncio import sleep
from dataclasses import dataclass

from keystone.core.errors import ServiceError

RETRYABLE_ERRORS = frozenset(
    {
        "identity_busy",
        "file_capacity_busy",
        "file_busy",
        "database_connection_busy",
    }
)


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    interval_seconds: float = 3
    backoff_rate: float = 2
    timeout_seconds: float | None = None

    def __post_init__(self):
        if type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 10:
            raise ValueError("Retry attempts must be between 1 and 10")
        for value, maximum in ((self.interval_seconds, 60), (self.backoff_rate, 4)):
            if (
                type(value) not in (int, float)
                or not math.isfinite(value)
                or not 1 <= value <= maximum
            ):
                raise ValueError("Invalid bounded retry interval or backoff")
        if self.timeout_seconds is not None and (
            type(self.timeout_seconds) not in (int, float)
            or not math.isfinite(self.timeout_seconds)
            or not 0 < self.timeout_seconds <= 86400
        ):
            raise ValueError("Invalid retry attempt timeout")


def retryable(error):
    return isinstance(error, ServiceError) and error.code in RETRYABLE_ERRORS


def safe_retry_error(error):
    # Only approved codes and fixed text may reach native retry logs/checkpoints.
    return ServiceError(error.status, error.code, "临时资源繁忙，请稍后重试")


async def retry_operation(operation, *args, policy=None, check=None, retry_if=retryable, **kwargs):
    """Caller proves idempotency and unwinds resources before each exception.

    Use for a local save after a model reply, not for replaying its model request.
    Whole durable steps should use the native safe_step retry adapter instead.
    """
    policy = policy or RetryPolicy()
    for attempt in range(policy.max_attempts):
        try:
            async with asyncio.timeout(policy.timeout_seconds):
                if check is not None:
                    await check()
                return await operation(*args, **kwargs)
        except ServiceError as error:
            if not retry_if(error):
                raise
            if attempt + 1 == policy.max_attempts:
                raise safe_retry_error(error) from None
        await sleep(min(3600, policy.interval_seconds * policy.backoff_rate**attempt))

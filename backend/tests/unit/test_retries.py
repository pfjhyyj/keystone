import asyncio

import pytest

from keystone.core import retries
from keystone.core.errors import ServiceError
from keystone.core.execution.steps import safe_step
from keystone.core.retries import RetryPolicy, retry_operation


async def test_local_reply_save_retries_without_repeating_generation(monkeypatch):
    waits, attempts, checks = [], [], []
    reply = b"one already generated reply"

    async def sleep(seconds):
        waits.append(seconds)

    async def check():
        checks.append(True)

    async def save(content):
        attempts.append(content)
        if len(attempts) < 3:
            raise ServiceError(503, "identity_busy", "untrusted diagnostic text")
        return "same-file-id"

    monkeypatch.setattr(retries, "sleep", sleep)
    assert await retry_operation(save, reply, check=check) == "same-file-id"
    assert waits == [3, 6]
    assert attempts == [reply] * 3 and len(checks) == 3


@pytest.mark.parametrize(
    "code",
    [
        "file_capacity_full",
        "file_scope_busy",
        "model_timeout",
        "model_result_unavailable",
        "execution_not_authorized",
    ],
)
async def test_permanent_or_unknown_results_are_not_retried(code):
    attempts = []

    async def fail():
        attempts.append(True)
        raise ServiceError(507, code, "safe failure")

    with pytest.raises(ServiceError) as error:
        await retry_operation(fail)
    assert error.value.code == code and len(attempts) == 1


async def test_cancellation_during_backoff_stops_new_attempts(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    attempts = []

    async def sleep(_seconds):
        entered.set()
        await release.wait()

    async def fail():
        attempts.append(True)
        raise ServiceError(503, "identity_busy", "busy")

    monkeypatch.setattr(retries, "sleep", sleep)
    task = asyncio.create_task(retry_operation(fail))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(attempts) == 1


@pytest.mark.parametrize(
    "values",
    [
        {"max_attempts": 0},
        {"max_attempts": True},
        {"interval_seconds": float("nan")},
        {"backoff_rate": float("inf")},
        {"timeout_seconds": 0},
    ],
)
def test_policy_is_finite_and_retries_require_explicit_idempotency(values):
    with pytest.raises(ValueError):
        RetryPolicy(**values)
    with pytest.raises(ValueError, match="idempotency"):
        safe_step(name="unsafe", retries_allowed=True)

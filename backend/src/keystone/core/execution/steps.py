"""Sanitize exceptions before DBOS records a step failure."""

import logging
from functools import wraps

from dbos import DBOS
from dbos import error as dbos_error

from keystone.core.errors import ServiceError
from keystone.core.logging import safe_exception
from keystone.core.retries import RetryPolicy, retryable, safe_retry_error

log = logging.getLogger(__name__)


def safe_step(
    *,
    name: str,
    retries_allowed: bool = False,
    max_attempts: int = 3,
    error_code: str = "task_step_failed",
    idempotent: bool = False,
    retry_policy: RetryPolicy | None = None,
    before_attempt=None,
    preserve_errors=frozenset(),
    preemptible: bool = False,
    timeout_seconds: float | None = None,
):
    if (retries_allowed or retry_policy is not None) and not idempotent:
        raise ValueError("Step retries require explicit idempotency")
    policy = retry_policy or RetryPolicy(max_attempts=max_attempts, timeout_seconds=timeout_seconds)
    enabled = retries_allowed or retry_policy is not None

    def decorate(function):
        @wraps(function)
        async def sanitized(*args, **kwargs):
            try:
                if preemptible and DBOS.workflow_id is not None:
                    # Native retry sleeps sit outside the per-attempt supervisor.
                    # A quick next attempt must not outrun its cancellation poller.
                    status = await DBOS.get_workflow_status_async(DBOS.workflow_id)
                    if status is not None and status.status == "CANCELLED":
                        raise dbos_error.DBOSWorkflowCancelledError("Execution cancelled")
                if before_attempt is not None:
                    await before_attempt(*args, **kwargs)
                return await function(*args, **kwargs)
            except Exception as error:
                if enabled and retryable(error):
                    raise safe_retry_error(error) from None
                if isinstance(error, ServiceError) and error.code in preserve_errors:
                    raise ServiceError(error.status, error.code, "操作未完成") from None
                log.error(
                    "%s type=%s",
                    error_code,
                    type(error).__name__,
                    extra={"exception": safe_exception(error)},
                )
                raise RuntimeError(error_code) from None

        return DBOS.step(
            name=name,
            retries_allowed=enabled,
            max_attempts=policy.max_attempts,
            interval_seconds=policy.interval_seconds,
            backoff_rate=policy.backoff_rate,
            should_retry=retryable,
            preemptible=preemptible,
            timeout_seconds=policy.timeout_seconds,
        )(sanitized)

    return decorate

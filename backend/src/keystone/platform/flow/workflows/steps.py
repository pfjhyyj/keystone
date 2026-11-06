"""Native Step adapter with a fresh Run check before every idempotent attempt."""

from keystone.core.execution.steps import safe_step
from keystone.platform.flow.schemas.errors import ERRORS
from keystone.platform.flow.workflows.context import flow_context


def flow_step(*, name, retry_policy=None, error_code="flow_execution_failed", timeout_seconds=None):
    async def check(run_id, *args, **kwargs):
        await flow_context(run_id).check()

    return safe_step(
        name=name,
        retry_policy=retry_policy,
        idempotent=retry_policy is not None,
        before_attempt=check,
        preserve_errors=ERRORS,
        preemptible=True,
        timeout_seconds=timeout_seconds,
        error_code=error_code,
    )

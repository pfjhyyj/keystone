"""Offline recovery only; never infer process death from the age of an invocation."""

from keystone.core.errors import ServiceError
from keystone.platform.model_access.services.recovery import recover_calls


async def recover_api_calls(database, *, api_stopped=False):
    # Operator assertion, not process detection. All API processes must stay stopped.
    if not api_stopped:
        raise ServiceError(409, "api_stop_required", "请先停止全部 API 进程并明确确认")
    return await recover_calls(database, flow=False, source="offline_api_recovery")

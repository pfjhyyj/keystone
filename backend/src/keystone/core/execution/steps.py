"""Sanitize exceptions before DBOS records a step failure."""

import logging
from functools import wraps

from dbos import DBOS

from keystone.core.logging import safe_exception

log = logging.getLogger(__name__)


def safe_step(
    *,
    name: str,
    retries_allowed: bool = False,
    max_attempts: int = 3,
    error_code: str = "task_step_failed",
):
    def decorate(function):
        @wraps(function)
        async def sanitized(*args, **kwargs):
            try:
                return await function(*args, **kwargs)
            except Exception as error:
                log.error(
                    "%s type=%s",
                    error_code,
                    type(error).__name__,
                    extra={"exception": safe_exception(error)},
                )
                raise RuntimeError(error_code) from None

        return DBOS.step(
            name=name,
            retries_allowed=retries_allowed,
            max_attempts=max_attempts,
        )(sanitized)

    return decorate

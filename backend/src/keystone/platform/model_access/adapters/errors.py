"""Translate protocol failures at the Keystone host boundary."""

from contextlib import contextmanager

from keystone.core.errors import ServiceError
from keystone.platform.model_access.schemas.errors import AdapterError


@contextmanager
def adapter_errors():
    try:
        yield
    except AdapterError as error:
        raise ServiceError(error.status, error.code, error.detail) from None

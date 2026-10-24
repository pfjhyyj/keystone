"""Execution declarations, safe error projection and cancellation without SQL."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from keystone.core.errors import ServiceError
from keystone.platform.flow.adapters.dbos import Engine
from keystone.platform.flow.workflows import context as host

HOLD_SECONDS = 30


@pytest.mark.parametrize("permissions", ["audit:read", ("",), (123,)])
async def test_invalid_permission_declarations(permissions):
    with pytest.raises(ValueError):
        async with host._execution_scope(uuid4(), permissions=permissions):
            pytest.fail("Invalid declaration")


@pytest.mark.parametrize(
    "code",
    [
        "process_timeout",
        "process_output_limit",
        "process_file_limit",
        "process_unavailable",
    ],
)
@pytest.mark.parametrize("raw", [False, True])
async def test_engine_projects_only_exact_safe_process_codes(code, raw):
    engine = Engine.__new__(Engine)
    engine.client = SimpleNamespace(
        list_workflows_async=AsyncMock(
            return_value=[
                SimpleNamespace(
                    error=f"{code}: private native stderr"
                    if raw
                    else ServiceError(503, code, "private"),
                    status="ERROR",
                    output=None,
                    app_version="version",
                    name="flow",
                )
            ]
        )
    )
    result = await engine.state(uuid4())
    assert result["error_code"] == ("flow_execution_failed" if raw else code)
    assert "private" not in str(result)


async def test_monitor_cleanup_cancellation_cannot_replace_business_failure(monkeypatch):
    parent, entered = asyncio.current_task(), asyncio.Event()

    class Identity:
        @asynccontextmanager
        async def access(self):
            yield None, None
            if asyncio.current_task() is not parent:
                entered.set()
                try:
                    await asyncio.sleep(HOLD_SECONDS)
                except asyncio.CancelledError:
                    parent.cancel()  # A second cancellation arriving during watcher cleanup.
                    raise

    monkeypatch.setattr(host, "_identity", lambda _: Identity())
    failure = ValueError("business failure")
    with pytest.raises(ValueError) as error:
        async with host._execution_scope(uuid4()):
            await entered.wait()
            raise failure
    assert error.value is failure

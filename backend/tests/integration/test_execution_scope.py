"""Fresh execution authorization, cancellation and nested scope boundaries over real SQL."""

import asyncio
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import pytest
from sqlalchemy import update
from test_files_dictionary import client as client
from test_flow_artifacts import artifacts as artifacts
from test_flow_artifacts import start
from test_identity import bearer, query, token
from test_processes import python, ready, stopped, tree

from keystone.core.errors import ServiceError
from keystone.core.time import utcnow
from keystone.platform.flow.models import FlowRun
from keystone.platform.flow.workflows import context as host

pytestmark = pytest.mark.postgres

# Cancellation must be prompt. A finite sentinel turns a broken cancel path into
# a bounded test failure instead of a ten-minute hang.
HOLD_SECONDS = 30


def change(client, run_id, **values):
    query(
        client,
        lambda session: session.execute(
            update(FlowRun).where(FlowRun.id == UUID(run_id)).values(**values)
        ),
    )


@pytest.mark.parametrize(
    "values,code",
    [
        ({"cancel_requested_at": utcnow()}, "flow_cancelled"),
        ({"status": "failed", "finished_at": utcnow()}, "flow_cancelled"),
        ({"execution_version": "0" * 64}, "execution_version_unavailable"),
    ],
)
def test_scope_checks_before_entry(artifacts, values, code):
    client, run_id, _ = artifacts
    change(client, run_id, **values)

    async def operation():
        async with host.execution_scope(run_id):
            pytest.fail("Unauthorized scope must never execute its body")

    with pytest.raises(ServiceError) as error:
        client.portal.call(operation)
    assert error.value.code == code


def test_missing_run_and_extra_permission_at_entry(artifacts):
    client, run_id, source = artifacts
    issued = token(client, ["flow:start", "file:create", "file:content_read"])
    pat_run = start(client, source, "limited", bearer(issued["token"]))

    async def operation(identifier, permissions):
        async with host.execution_scope(identifier, permissions=permissions):
            pytest.fail("Invalid authority must never reach business code")

    for identifier, permissions, code in (
        (str(uuid4()), (), "flow_not_found"),
        (pat_run, ("audit:read",), "permission_denied"),
    ):
        with pytest.raises(ServiceError) as error:
            client.portal.call(operation, identifier, permissions)
        assert error.value.code == code
    assert client.delete(f"/api/v1/auth/tokens/{issued['id']}").status_code == 204
    with pytest.raises(ServiceError) as error:
        client.portal.call(operation, pat_run, ())
    assert error.value.code == "execution_not_authorized"


@pytest.mark.parametrize("explicit_check", [False, True])
def test_quick_change_detected_at_boundary_without_a_monitor_tick(artifacts, explicit_check):
    client, run_id, _ = artifacts

    async def operation():
        async with host.execution_scope(run_id) as check:
            async with client.app.state.runtime.database.transaction() as session:
                await session.execute(
                    update(FlowRun)
                    .where(FlowRun.id == UUID(run_id))
                    .values(cancel_requested_at=utcnow())
                )
            if explicit_check:
                await check()

    with pytest.raises(ServiceError) as error:
        client.portal.call(operation)
    assert error.value.code == "flow_cancelled"


@pytest.mark.parametrize("revoke", [False, True])
def test_long_process_cancel_or_pat_revocation_releases_materialized_file(
    artifacts,
    tmp_path,
    revoke,
):
    client, run_id, source = artifacts
    issued = token(client, ["flow:start", "file:create", "file:content_read", "audit:read"])
    run_id = start(client, source, "process", bearer(issued["token"]))
    argv, heartbeat, gate = tree(tmp_path)
    local = []

    async def operation():
        async with host.execution_scope(run_id, permissions=("audit:read",)):
            async with host.materialize_file(run_id, source) as path:
                local.append(path)
                await host.run_process(None, argv)

    future = client.portal.start_task_soon(operation)
    try:
        client.portal.call(ready, gate)
        if revoke:
            assert client.delete(f"/api/v1/auth/tokens/{issued['id']}").status_code == 204
        else:
            # This write must complete while the scope is open: no long SQL transaction.
            change(client, run_id, cancel_requested_at=utcnow())
        with pytest.raises(ServiceError) as error:
            future.result(timeout=15)
    finally:
        if not future.done():
            future.cancel()
    assert error.value.code == ("execution_not_authorized" if revoke else "flow_cancelled")
    assert local and not local[0].exists()
    client.portal.call(stopped, heartbeat)


def test_nested_scope_keeps_outer_requirements_and_rejects_other_run(artifacts):
    client, run_id, source = artifacts
    other = start(client, source, "other")

    async def operation():
        async with host.execution_scope(run_id, permissions=("audit:read",)):
            async with host.execution_scope(run_id):
                assert host._execution.get()[1] == frozenset({"audit:read"})
                result = await host.run_process(None, python("print('ok')"))
                assert result.stdout.strip() == b"ok"
                with pytest.raises(ServiceError) as error:
                    async with host.execution_scope(other):
                        pytest.fail("Cross-run nested scope")
                assert error.value.code == "flow_not_found"
        assert host._execution.get() is None
        with pytest.raises(RuntimeError, match="active execution scope"):
            await host.run_process(None, python("pass"))

    client.portal.call(operation)


def test_business_failure_is_preserved_and_external_cancel_stays_cancelled(artifacts):
    client, run_id, _ = artifacts

    async def operation():
        failure = ValueError("business failure")
        with pytest.raises(ValueError) as error:
            async with host.execution_scope(run_id):
                raise failure
        assert error.value is failure
        entered = asyncio.Event()

        async def hold():
            async with host.execution_scope(run_id):
                entered.set()
                await asyncio.sleep(HOLD_SECONDS)

        task = asyncio.create_task(hold())
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert host._execution.get() is None

    client.portal.call(operation)


def test_monitor_retries_transient_dependency_failure_without_logging_secrets(
    artifacts,
    monkeypatch,
    caplog,
):
    client, run_id, _ = artifacts
    original = host.identity
    checks = []

    class Flaky:
        @asynccontextmanager
        async def access(self):
            checks.append(1)
            if len(checks) == 2:
                raise RuntimeError("secret connection string")
            async with original(run_id).access() as value:
                yield value

    monkeypatch.setattr(host, "identity", lambda _: Flaky())

    async def operation():
        async with host.execution_scope(run_id):
            await asyncio.sleep(2.2)

    client.portal.call(operation)
    assert len(checks) >= 4  # entry, failing tick, recovered tick, exit
    assert "execution_monitor_retry type=RuntimeError" in caplog.text
    assert "secret connection string" not in caplog.text

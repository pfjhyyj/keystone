"""Run file quotas and live capacity/temporary ownership over real SQL, files and processes."""

import asyncio

import pytest
from sqlalchemy import select
from test_files_dictionary import client as client
from test_flow_budgets import accept
from test_identity import query
from test_processes import python, ready, stopped, tree

from keystone.core.advisory import lock_key, session_lease
from keystone.core.errors import ServiceError
from keystone.platform.files.models import File
from keystone.platform.flow.models import FlowRunBudget
from keystone.platform.flow.schemas.budgets import FlowLimits
from keystone.platform.flow.workflows import context as host

pytestmark = pytest.mark.postgres


def context(client, monkeypatch, limits):
    response, _ = accept(client, limits=limits)
    assert response.status_code == 202, response.text
    monkeypatch.setattr(host, "_bound_runtime", client.app.state.runtime)
    return host.flow_context(response.json()["id"])


def test_artifact_count_bytes_replay_and_conflict_stop_before_storage_write(client, monkeypatch):
    ctx = context(client, monkeypatch, FlowLimits(file_count=1, file_bytes=4, temporary_bytes=8))

    async def exercise():
        file_id = await ctx.upload("one", "one.txt", b"1234", content_type="text/plain")
        assert await ctx.upload("one", "one.txt", b"1234", content_type="text/plain") == file_id
        async with ctx.workspace(max_bytes=4) as path:
            path.write_bytes(b"1234")
            assert (
                await ctx.upload_file("one", path, name="one.txt", content_type="text/plain")
                == file_id
            )
        source = client.app.state.runtime.storage.get()

        def forbidden(*args, **kwargs):
            raise AssertionError("Quota rejection must precede storage writes")

        monkeypatch.setattr(source, "write", forbidden)
        for key, data, code in (
            ("two", b"1234", "flow_budget_exceeded"),
            ("one", b"changed", "flow_budget_conflict"),
        ):
            with pytest.raises(ServiceError) as error:
                await ctx.upload(key, "one.txt", data, content_type="text/plain")
            assert error.value.code == code
        assert await ctx.read_file(file_id, max_bytes=4) == b"1234"
        return file_id

    identifier = client.portal.call(exercise)
    assert len(query(client, lambda s: s.scalars(select(File))).all()) == 1
    budget = query(client, lambda s: s.get(FlowRunBudget, ctx.run_id))
    assert budget.counters["file_count"] == 1 and budget.counters["file_bytes"] == 4
    assert client.get(f"/api/v1/files/{identifier}").status_code == 200


def test_nested_same_task_reuses_capacity_but_child_task_cannot_inherit_it(client, monkeypatch):
    ctx = context(client, monkeypatch, FlowLimits(concurrent_operations=1))

    async def hold():
        async with ctx.execution_scope():
            async with ctx.execution_scope():
                pass

    async def exercise():
        async with ctx.execution_scope():
            await hold()
            with pytest.raises(ServiceError) as error:
                await asyncio.create_task(hold())
            assert error.value.code == "flow_capacity_busy"
        await asyncio.create_task(hold())

    client.portal.call(exercise)


def test_parallel_tasks_share_capacity_and_release_on_cancel(client, monkeypatch):
    ctx = context(client, monkeypatch, FlowLimits(concurrent_operations=2))

    async def exercise():
        entered, release = [asyncio.Event(), asyncio.Event()], asyncio.Event()

        async def hold(index):
            async with ctx.execution_scope():
                entered[index].set()
                await release.wait()

        tasks = [asyncio.create_task(hold(index)) for index in range(2)]
        try:
            await asyncio.gather(*(item.wait() for item in entered))
            with pytest.raises(ServiceError) as error:
                async with ctx.execution_scope():
                    pytest.fail("Third operation exceeded the Run capacity")
            assert error.value.code == "flow_capacity_busy"
        finally:
            for task in tasks:
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        async with ctx.execution_scope():
            pass

    client.portal.call(exercise)


def test_process_file_output_requires_current_task_managed_workspace(client, monkeypatch, tmp_path):
    ctx = context(client, monkeypatch, FlowLimits(temporary_bytes=4))
    denied = tmp_path / "denied"

    async def exercise():
        with pytest.raises(ServiceError) as error:
            await ctx.run_process(
                python(f"open({str(denied)!r},'w').write('bad')"), output=denied, output_limit=4
            )
        assert error.value.code == "flow_process_output_denied" and not denied.exists()
        async with ctx.workspace(max_bytes=4) as output:
            with pytest.raises(ServiceError):
                async with ctx.workspace(max_bytes=1):
                    pytest.fail("Two workspaces exceeded the shared temporary quota")

            async def child():
                return await ctx.run_process(python("pass"), output=output, output_limit=4)

            with pytest.raises(ServiceError) as inherited:
                await asyncio.create_task(child())
            assert inherited.value.code == "flow_process_output_denied"
            result = await ctx.run_process(
                python(f"open({str(output)!r},'wb').write(b'safe')"), output=output, output_limit=4
            )
            assert result.returncode == 0 and output.read_bytes() == b"safe"
        assert not output.exists()
        with pytest.raises(ServiceError):
            await ctx.run_process(python("pass"), output=output, output_limit=4)

    client.portal.call(exercise)
    root = client.app.state.runtime.storage.coordination.root
    assert not list((root / ".capacity").iterdir()) and not list((root / ".transfers").iterdir())


def test_lost_run_permit_cancels_process_and_cleans_workspace(client, monkeypatch, tmp_path):
    from sqlalchemy import text

    ctx = context(client, monkeypatch, FlowLimits(concurrent_operations=1, temporary_bytes=4))
    argv, heartbeat, gate = tree(tmp_path)
    paths = []

    async def exercise():
        async with ctx.workspace(max_bytes=4) as path:
            paths.append(path)
            await ctx.run_process(argv)

    future = client.portal.start_task_soon(exercise)
    try:
        client.portal.call(ready, gate)
        key = lock_key(f"flow-operations:{ctx.run_id}:0") % 2**64

        async def disconnect(session):
            return await session.scalar(
                text("""SELECT pg_terminate_backend(pid) FROM pg_locks
                WHERE locktype='advisory' AND classid=:upper AND objid=:lower AND objsubid=1
                AND granted AND pid <> pg_backend_pid()"""),
                {"upper": key >> 32, "lower": key & 0xFFFFFFFF},
            )

        assert query(client, disconnect)
        with pytest.raises(ServiceError) as error:
            future.result(timeout=15)
        assert error.value.code == "flow_operation_interrupted"
    finally:
        if not future.done():
            future.cancel()
    client.portal.call(stopped, heartbeat)
    assert paths and not paths[0].exists()

    async def released():
        async with session_lease(
            client.app.state.runtime.database, capacities=[(f"flow-operations:{ctx.run_id}", 1)]
        ):
            pass

    client.portal.call(released)


def test_cancellation_drains_process_before_releasing_full_workspace_and_permit(
    client, monkeypatch, tmp_path
):
    from sqlalchemy import update

    from keystone.core.time import utcnow
    from keystone.platform.flow.models import FlowRun

    ctx = context(client, monkeypatch, FlowLimits(concurrent_operations=1, temporary_bytes=4))
    argv, heartbeat, gate = tree(tmp_path)
    paths = []

    async def exercise():
        async with ctx.workspace(max_bytes=4) as path:
            paths.append(path)
            path.write_bytes(b"held")
            await ctx.run_process(argv)

    future = client.portal.start_task_soon(exercise)
    try:
        client.portal.call(ready, gate)
        query(
            client,
            lambda s: s.execute(
                update(FlowRun).where(FlowRun.id == ctx.run_id).values(cancel_requested_at=utcnow())
            ),
        )
        with pytest.raises(ServiceError) as error:
            future.result(timeout=15)
        assert error.value.code == "flow_cancelled"
    finally:
        if not future.done():
            future.cancel()
    client.portal.call(stopped, heartbeat)
    root = client.app.state.runtime.storage.coordination.root
    assert paths and not paths[0].exists()
    assert not list((root / ".capacity").iterdir()) and not list((root / ".transfers").iterdir())

    async def released():
        async with session_lease(
            client.app.state.runtime.database, capacities=[(f"flow-operations:{ctx.run_id}", 1)]
        ):
            pass

    client.portal.call(released)

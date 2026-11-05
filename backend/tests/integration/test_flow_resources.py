"""Managed process outputs and cancellation over real SQL, files and processes."""

import asyncio

import pytest
from support.flow_admission import accept
from test_files_dictionary import client as client
from test_identity import query
from test_processes import python, ready, stopped, tree

from keystone.core.errors import ServiceError
from keystone.platform.flow.workflows import context as host

pytestmark = pytest.mark.postgres


def context(client, monkeypatch):
    response, _ = accept(client)
    assert response.status_code == 202, response.text
    monkeypatch.setattr(host, "_bound_runtime", client.app.state.runtime)
    return host.flow_context(response.json()["id"])


def test_process_file_output_requires_current_task_managed_workspace(client, monkeypatch, tmp_path):
    ctx = context(client, monkeypatch)
    denied = tmp_path / "denied"

    async def exercise():
        with pytest.raises(ServiceError) as error:
            await ctx.run_process(
                python(f"open({str(denied)!r},'w').write('bad')"), output=denied, output_limit=4
            )
        assert error.value.code == "flow_process_output_denied" and not denied.exists()
        async with ctx.workspace(max_bytes=4) as directory:
            output = directory / "output"

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


def test_cancellation_drains_process_before_releasing_workspace(client, monkeypatch, tmp_path):
    from sqlalchemy import update

    from keystone.core.time import utcnow
    from keystone.platform.flow.models import FlowRun

    ctx = context(client, monkeypatch)
    argv, heartbeat, gate = tree(tmp_path)
    paths = []

    async def exercise():
        async with ctx.workspace(max_bytes=4) as directory:
            path = directory / "output"
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

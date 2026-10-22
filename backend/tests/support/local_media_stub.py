"""Isolated 0A acceptance flow. No business registration or model provider required."""

import asyncio
import os
from pathlib import Path

from keystone.core.schemas import Input
from keystone.platform.flow.schemas.definitions import Definition


class Source(Input):
    source_file_id: str
    wait_for: str | None = None


class Result(Input):
    file_id: str


definition = Definition(
    key="test.local_media",
    name="Local media acceptance",
    description="Local transfer only",
    input_type=Source,
    output_type=Result,
    entrypoint="test.local_media.run",
    required_permissions=("file:create", "file:content_read"),
    file_fields=("source_file_id",),
    output_file_fields=("file_id",),
    timeout_seconds=3600,
)


def register():
    from dbos import DBOS

    from keystone.platform.flow.workflows import context as host

    @DBOS.step(name="test.local_media.copy", retries_allowed=False)
    async def copy(run_id):
        inputs = await host.inputs(run_id)
        async with host.materialize_file(run_id, inputs["source_file_id"]) as path:
            gate = inputs.get("wait_for")
            if gate:
                Path(gate + ".entered").touch()
                if os.environ.get("KEYSTONE_ACCEPTANCE_CRASH") == "1":
                    os._exit(71)
                while not Path(gate).exists():
                    await asyncio.sleep(0.1)
            identifier = await host.upload_file(
                run_id, "copy", path, name="copy.mp4", content_type="video/mp4"
            )
            return {"file_id": identifier}

    @DBOS.workflow(name=definition.entrypoint)
    async def run(run_id):
        return await copy(run_id)


def install():
    from keystone import composition

    composition.FLOW_DEFINITIONS = (definition,)
    composition.register_workflows = register
    if os.environ.get("KEYSTONE_ACCEPTANCE_CANCEL_FAIL") == "1":
        from keystone.platform.flow.adapters.dbos import Engine

        async def unavailable(self, identifier):
            raise RuntimeError("Engine cancellation unavailable")

        Engine.cancel = unavailable

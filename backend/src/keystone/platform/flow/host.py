"""Only trusted runtime handles live here. DBOS checkpoints receive IDs and small values."""

import hashlib
from uuid import UUID

from keystone.platform.files.schemas import Upload
from keystone.platform.files.services.files import FileService
from keystone.platform.flow.identity import ExecutionIdentity
from keystone.platform.flow.models import FlowRun

_runtime = None


def bind(runtime):
    global _runtime
    _runtime = runtime


def runtime():
    if _runtime is None:
        raise RuntimeError("Flow host is not bound")
    return _runtime


def identity(run_id):
    return ExecutionIdentity(runtime(), run_id)


async def inputs(run_id):
    async with identity(run_id).access() as (session, _):
        return (await session.get(FlowRun, UUID(run_id))).input


async def read_file(run_id, identifier, *, max_bytes):
    _, content = await FileService(identity(run_id), runtime().storage).content(
        UUID(identifier), max_bytes=max_bytes
    )
    return content


async def upload(run_id, key, name, content, *, content_type, storage_key=None):
    request_key = f"flow:{run_id}:{key}"
    result = await FileService(identity(run_id), runtime().storage).upload_referenced(
        Upload(
            storage_key=storage_key,
            name=name,
            content_type=content_type,
            expected_size=len(content),
            request_key=request_key,
            checksum_sha256=hashlib.sha256(content).hexdigest(),
        ),
        content,
        owner=f"flow:{run_id}",
        slot=f"artifact.{key}",
    )
    return str(result["id"])

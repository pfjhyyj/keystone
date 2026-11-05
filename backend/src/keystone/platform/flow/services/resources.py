"""Authorized resources for one Run; handles and local paths never become checkpoints."""

import hashlib
from contextlib import asynccontextmanager
from copy import deepcopy
from uuid import UUID

from keystone.core.errors import ServiceError
from keystone.platform.files.schemas import Upload
from keystone.platform.files.services.files import FileService
from keystone.platform.files.services.references import require_usage
from keystone.platform.flow.models import FlowRun
from keystone.platform.flow.schemas.snapshots import read_snapshot
from keystone.platform.flow.services.artifacts import FlowArtifactService, artifact_location


class FlowResourceService:
    def __init__(self, identity, storage):
        self.identity, self.storage = identity, storage
        self.files = FileService(identity, storage)

    async def inputs(self):
        async with self.identity.access() as (session, _):
            return deepcopy((await session.get(FlowRun, self.identity.run_id)).input)

    async def require_file(self, identifier):
        async with self.identity.access() as (session, _):
            try:
                await require_usage(session, f"flow:{self.identity.run_id}", UUID(str(identifier)))
            except ServiceError as error:
                if error.code == "file_usage_denied":
                    raise ServiceError(
                        403, "flow_file_scope_denied", "文件不属于本次运行"
                    ) from None
                raise

    async def model_binding(self, role):
        async with self.identity.access("model:invoke") as (session, _):
            run = await session.get(FlowRun, self.identity.run_id)
            snapshot = read_snapshot(run.definition_snapshot)
            binding = snapshot["model_bindings"].get(role)
            if binding is None:
                raise ServiceError(409, "flow_model_role_unavailable", "模型角色未绑定")
            return binding

    async def read_file(self, identifier, *, max_bytes):
        await self.require_file(identifier)
        _, content = await self.files.content(UUID(str(identifier)), max_bytes=max_bytes)
        return content

    async def image_content(self, identifier, *, max_bytes):
        await self.require_file(identifier)
        async with self.files.open_content(UUID(str(identifier)), max_bytes=max_bytes) as reader:
            content = bytearray()
            async for chunk in reader.chunks():
                content.extend(chunk)
                if len(content) > max_bytes:
                    raise ServiceError(413, "file_too_large", "图片超过读取上限")
            content_type = reader.content_type
        await self.require_file(identifier)
        return content_type, bytes(content)

    async def upload(self, key, name, content, *, content_type, storage_key=None):
        owner, slot = artifact_location(self.identity.run_id, key)
        request_key = f"{owner}:{key}"
        data = Upload(
            storage_key=storage_key,
            name=name,
            content_type=content_type,
            expected_size=len(content),
            request_key=request_key,
            checksum_sha256=hashlib.sha256(content).hexdigest(),
        )
        self.files.prepare_upload(data.expected_size, storage_key)
        result = await self.files.upload_referenced(
            data,
            content,
            owner=owner,
            slot=slot,
        )
        return str(result["id"])

    async def artifact(self, key, *, max_bytes=None):
        return await FlowArtifactService(self.identity, self.storage).find(
            self.identity.run_id, key, max_bytes=max_bytes
        )

    async def artifacts(self, keys, *, max_bytes=None):
        return await FlowArtifactService(self.identity, self.storage).find_many(
            self.identity.run_id, keys, max_bytes=max_bytes
        )

    @asynccontextmanager
    async def local_file(self, identifier):
        await self.require_file(identifier)
        async with self.files.local_file(UUID(str(identifier))) as local:
            await self.require_file(identifier)
            yield local

    async def upload_file(self, key, path, *, name, content_type, transfer, storage_key=None):
        owner, slot = artifact_location(self.identity.run_id, key)

        result = await transfer.upload_file(
            path,
            storage_key=storage_key,
            name=name,
            content_type=content_type,
            request_key=f"{owner}:{key}",
            owner=owner,
            slot=slot,
        )
        return str(result["id"])

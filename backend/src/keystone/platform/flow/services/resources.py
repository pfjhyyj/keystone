"""Authorized resources for one Run; handles and local paths never become checkpoints."""

import hashlib
from uuid import UUID

from keystone.platform.files.schemas import Upload
from keystone.platform.files.services.files import FileService
from keystone.platform.flow.models import FlowRun
from keystone.platform.flow.services.artifacts import FlowArtifactService, artifact_location


class FlowResourceService:
    def __init__(self, identity, storage):
        self.identity, self.storage = identity, storage
        self.files = FileService(identity, storage)

    async def inputs(self):
        async with self.identity.access() as (session, _):
            return (await session.get(FlowRun, self.identity.run_id)).input

    async def read_file(self, identifier, *, max_bytes):
        _, content = await self.files.content(UUID(identifier), max_bytes=max_bytes)
        return content

    async def upload(self, key, name, content, *, content_type, storage_key=None):
        owner, slot = artifact_location(self.identity.run_id, key)
        request_key = f"{owner}:{key}"
        result = await self.files.upload_referenced(
            Upload(
                storage_key=storage_key,
                name=name,
                content_type=content_type,
                expected_size=len(content),
                request_key=request_key,
                checksum_sha256=hashlib.sha256(content).hexdigest(),
            ),
            content,
            owner=owner,
            slot=slot,
        )
        return str(result["id"])

    async def artifact(self, key, *, max_bytes=None):
        return await FlowArtifactService(self.identity, self.storage).find(
            self.identity.run_id, key, max_bytes=max_bytes
        )

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

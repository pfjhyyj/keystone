"""Run-scoped artifact queries and transaction checks; publication belongs to business."""

from uuid import UUID

from sqlalchemy import select

from keystone.core.errors import ServiceError
from keystone.platform.files.services.files import FileService
from keystone.platform.files.services.references import registered_file_ids, require_registered_file
from keystone.platform.flow.models import FlowRun
from keystone.platform.flow.services.identity import ExecutionIdentity, authorize_execution
from keystone.platform.flow.services.runs import scope


def artifact_location(run_id, key):
    # 128-char upload request key: "flow:" + UUID + ":" + at most 86 chars.
    # Keep logical keys opaque; they never become filesystem paths.
    if (
        not isinstance(key, str)
        or not 1 <= len(key) <= 86
        or any(ord(char) < 32 or ord(char) == 127 for char in key)
    ):
        raise ValueError("Artifact key must contain 1-86 printable characters")
    return f"flow:{UUID(str(run_id))}", f"artifact.{key}"


class FlowArtifactService:
    def __init__(self, identity, storage):
        self.identity, self.storage = identity, storage

    async def _authorize(self, session, auth, run_id):
        identifier = UUID(str(run_id))
        execution = isinstance(self.identity, ExecutionIdentity)
        if execution and identifier != self.identity.run_id:
            raise ServiceError(404, "flow_not_found", "流程不存在或不可访问")
        filters = [] if execution else scope(auth)
        run = await session.scalar(
            select(FlowRun)
            .where(FlowRun.id == identifier, *filters)
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
        if run is None:
            raise ServiceError(404, "flow_not_found", "流程不存在或不可访问")
        if execution:
            # Never widen a Run's frozen PAT scope using the caller's session auth.
            auth = await authorize_execution(
                session,
                run,
                self.identity.flow_definitions,
                execution_version=self.identity.execution_version(),
            )
        auth.require("file:content_read")
        return auth

    async def _candidate(self, session, run_id, key):
        owner, slot = artifact_location(run_id, key)
        identifiers = await registered_file_ids(session, owner, slot)
        if len(identifiers) > 1:
            raise ServiceError(409, "flow_artifact_ambiguous", "运行产物键对应多个文件")
        return identifiers[0] if identifiers else None

    async def find(self, run_id, key, *, max_bytes=None):
        """Return a verified file ID or None only when no artifact is registered.

        Current Run/file authorization and local integrity (or bounded S3 reads)
        come from the existing services. No DB transaction remains open during I/O.
        """
        artifact_location(run_id, key)
        async with self.identity.access() as (session, auth):
            await self._authorize(session, auth, run_id)
            identifier = await self._candidate(session, run_id, key)
        if identifier is None:
            return None
        async with FileService(self.identity, self.storage).open_content(
            identifier, max_bytes=max_bytes
        ):
            # A reference may have changed while hashing/opening. Recheck before receipt.
            async with self.identity.access() as (session, auth):
                await self.require_in_transaction(session, auth, run_id, {key: identifier})
        return str(identifier)

    async def require_in_transaction(self, session, auth, run_id, artifacts):
        """Check exact key->ID claims in the caller's authorized business transaction.

        The caller holds identity and business-object locks and decides completeness,
        spec, distinctness and publication. SQL only: no byte scan, commit, reference
        mutation or grants. Hold file locks in ID order until the caller commits.
        """
        if len(artifacts) > 100:
            raise ValueError("At most 100 artifact claims are supported")
        claims = [
            (key, UUID(str(identifier)), artifact_location(run_id, key))
            for key, identifier in artifacts.items()
        ]
        auth = await self._authorize(session, auth, run_id)
        for key, identifier, (owner, slot) in sorted(claims, key=lambda item: (item[1], item[0])):
            if await self._candidate(session, run_id, key) != identifier:
                raise ServiceError(409, "flow_invalid_artifact", "文件不属于本次运行的指定产物键")
            try:
                await require_registered_file(session, auth, owner, slot, identifier)
            except ServiceError as error:
                if error.code == "file_reference_invalid":
                    raise ServiceError(
                        409, "flow_invalid_artifact", "运行产物引用已发生变化"
                    ) from None
                raise

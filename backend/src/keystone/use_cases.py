"""Explicit cross-domain use cases, assembled by bootstrap."""

from keystone.platform.flow.schemas.diagnostics import (
    Diagnostics,
    FlowFileDiagnostic,
    FlowFileDiagnostics,
    source_invocation,
)
from keystone.platform.model_access.schemas.diagnostics import ImageSource


class RoleDeletion:
    def __init__(self, identity, files):
        self.identity, self.files = identity, files

    async def execute(self, identifier):
        async with self.identity.context.access(
            "role:delete",
            write=True,
            action="identity.role.delete",
            target="identity.role",
            target_id=identifier,
        ) as (session, auth):
            role, permissions = await self.identity.prepare_role_deletion(session, auth, identifier)
            await self.files.ensure_role_unused(session, identifier)
            await self.identity.delete_role(session, auth, role, permissions)


class FlowCosts:
    """Keep Flow visibility checks around the separately authorized model summary."""

    def __init__(self, flows, models):
        self.flows, self.models = flows, models

    async def execute(self, identifier):
        await self.flows.detail(identifier)
        result = await self.models.summary(flow_run_id=identifier)
        await self.flows.detail(identifier)
        return result


class FlowDiagnostics:
    """Compose independently authorized facts in one bounded, identity-locked read."""

    def __init__(self, flows, models, files):
        self.flows, self.models, self.files = flows, models, files

    async def execute(self, identifier, offset=0, limit=50, section="all"):
        async with self.flows.identity.access("flow:read") as (session, auth):
            await self.flows.visible(session, auth, identifier)
            result = Diagnostics(run_id=identifier)
            if section in {"all", "budget"}:
                result.budget = await self.flows.budget_diagnostic_in_transaction(
                    session, auth, identifier
                )
            if section in {"all", "models"}:
                result.models = await self.models.flow_diagnostics_in_transaction(
                    session, auth, identifier, offset, limit
                )
            if section in {"all", "files"}:
                references = await self.files.flow_diagnostics_in_transaction(
                    session, auth, identifier, offset, limit
                )
                candidates = [
                    ImageSource(
                        file_id=row.file_id,
                        invocation_id=source,
                        artifact_key=row.slot.removeprefix("artifact."),
                        checksum_sha256=row.checksum_sha256,
                        size_bytes=row.size_bytes,
                        content_type=row.content_type,
                    )
                    for row in references.items
                    if (source := source_invocation(row.slot)) is not None
                ]
                confirmed = await self.models.image_sources_in_transaction(
                    session, auth, identifier, candidates
                )
                sources = {
                    (file_id, key): invocation_id for file_id, invocation_id, key in confirmed
                }
                result.files = FlowFileDiagnostics(
                    available=references.available,
                    truncated=references.truncated,
                    next_offset=references.next_offset,
                    items=[
                        FlowFileDiagnostic(
                            file_id=row.file_id,
                            slot=row.slot,
                            invocation_id=sources.get(
                                (row.file_id, row.slot.removeprefix("artifact."))
                            ),
                        )
                        for row in references.items
                    ],
                )
            await self.flows.visible(session, auth, identifier)
            return result

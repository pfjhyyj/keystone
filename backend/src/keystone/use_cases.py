"""Explicit cross-domain use cases, assembled by bootstrap."""

import asyncio
import logging

from keystone.core.errors import ServiceError
from keystone.core.time import utcnow
from keystone.platform.flow.schemas.diagnostics import (
    Diagnostics,
    FlowFileDiagnostic,
    FlowFileDiagnostics,
    source_invocation,
)
from keystone.platform.model_access.schemas.diagnostics import ImageSource


class PlatformStatus:
    def __init__(self, identity, workers, backlogs):
        self.identity, self.workers, self.backlogs = identity, workers, backlogs

    async def execute(self):
        if self.identity.credential is None:
            raise ServiceError(401, "authentication_required", "请先登录")
        async with self.identity.access():
            pass

        async def observe(probe):
            try:
                async with asyncio.timeout(3):
                    pending, oldest = await probe()
                return {
                    "status": "ok",
                    "pending": pending,
                    "oldest_pending_at": oldest.isoformat() if oldest else None,
                    "oldest_pending_age_seconds": max(0, int((utcnow() - oldest).total_seconds()))
                    if oldest
                    else None,
                }
            except Exception:
                return {
                    "status": "unknown",
                    "pending": None,
                    "oldest_pending_at": None,
                    "oldest_pending_age_seconds": None,
                }

        result, *values = await asyncio.gather(
            self.workers(), *(observe(probe) for probe in self.backlogs.values())
        )
        result["backlog"] = dict(zip(self.backlogs, values, strict=True))
        async with self.identity.access():
            pass
        return result


class HistoryRetention:
    def __init__(self, credentials, audit, engine):
        self.credentials, self.audit, self.engine = credentials, audit, engine

    async def execute(self, *, apply=False):
        result = {"applied": apply}
        try:
            async with asyncio.timeout(10):
                result.update(await self.credentials(apply=apply))
            async with asyncio.timeout(10):
                result["audit"] = await self.audit(apply=apply)
            async with asyncio.timeout(30):
                result["maintenance"] = await self.engine(apply=apply)
            result["status"] = "ok"
            return result
        finally:
            logging.getLogger(__name__).info(
                "history_retention",
                extra={
                    "diagnostic": {**result, "status": result.get("status", "partial_failure")},
                },
            )


class FlowCosts:
    """Keep Flow visibility checks around the separately authorized model summary."""

    def __init__(self, flows, models):
        self.flows, self.models = flows, models

    async def execute(self, identifier, *, cost_basis="actual"):
        await self.flows.detail(identifier)
        result = await self.models.summary(
            flow_run_id=identifier, **({"cost_basis": cost_basis} if cost_basis != "actual" else {})
        )
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
            if section in {"all", "models"}:
                result.models = await self.models.flow_diagnostics_in_transaction(
                    session, auth, identifier, offset, limit
                )
            if section in {"all", "files"}:
                references = await self.files.flow_diagnostics_in_transaction(
                    session, identifier, offset, limit
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

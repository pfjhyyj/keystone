"""Operator reads for explicit attempt records, approved attachments and output previews."""

from contextlib import asynccontextmanager

from pydantic import ValidationError
from sqlalchemy import select

from keystone.core.errors import ServiceError
from keystone.platform.files.services.references import (
    diagnostic_attempt_files,
    registered_file_ids,
    require_slot_usage,
    require_usage,
)
from keystone.platform.flow.models import FlowRun
from keystone.platform.flow.schemas.step_diagnostics import (
    ATTACHMENT_TYPES,
    IMAGE_ATTACHMENT_TYPES,
    MAX_ATTEMPTS,
    MAX_DETAIL_BYTES,
    MAX_IMAGE_PREVIEW_BYTES,
    MAX_PREVIEW_BYTES,
    StepAttempt,
    StepAttempts,
    attempt_prefix,
)
from keystone.platform.flow.services.artifacts import artifact_location


class StepDiagnosticService:
    def __init__(self, identity, files):
        self.identity, self.files = identity, files

    async def _require_run(self, session, run_id):
        if await session.scalar(select(FlowRun.id).where(FlowRun.id == run_id)) is None:
            raise ServiceError(404, "flow_not_found", "流程不存在或不可访问")

    async def _read_attempt(self, file_id):
        _, raw = await self.files.content(file_id, max_bytes=MAX_DETAIL_BYTES)
        try:
            return StepAttempt.model_validate_json(raw)
        except ValidationError:
            raise ServiceError(409, "flow_diagnostic_invalid", "阶段记录无法读取") from None

    async def attempts(self, run_id, workflow_id, step_id):
        prefix = artifact_location(run_id, attempt_prefix(workflow_id, step_id))[1]
        async with self.identity.access("flow:diagnose") as (session, _):
            await self._require_run(session, run_id)
            files = await diagnostic_attempt_files(
                session, f"flow:{run_id}", prefix, MAX_ATTEMPTS + 1
            )
        items = []
        for _, file_id in files[:MAX_ATTEMPTS]:
            try:
                items.append(await self._read_attempt(file_id))
            except ServiceError as error:
                if error.status not in {404, 410}:
                    raise
        async with self.identity.access("flow:diagnose") as (session, _):
            await self._require_run(session, run_id)
            current = await diagnostic_attempt_files(
                session, f"flow:{run_id}", prefix, MAX_ATTEMPTS + 1
            )
        if files != current:
            raise ServiceError(409, "flow_diagnostic_changed", "阶段记录已变化，请刷新")
        return StepAttempts(items=items, truncated=len(files) > MAX_ATTEMPTS)

    @asynccontextmanager
    async def _open_preview(self, identifier):
        async with self.files.open_content(identifier, max_bytes=MAX_IMAGE_PREVIEW_BYTES) as reader:
            if reader.content_type not in ATTACHMENT_TYPES:
                raise ServiceError(415, "flow_diagnostic_type", "此类型无法预览")
            if (
                reader.content_type not in IMAGE_ATTACHMENT_TYPES
                and reader.size > MAX_PREVIEW_BYTES
            ):
                raise ServiceError(413, "file_too_large", "文本或 JSON 预览超过 4 MiB 上限")
            yield reader

    @asynccontextmanager
    async def open_attachment(self, run_id, workflow_id, step_id, attempt, index):
        key = attempt_prefix(workflow_id, step_id) + f"{attempt:010d}"
        owner, slot = artifact_location(run_id, key)

        async def check(session, manifest=None, identifier=None):
            await self._require_run(session, run_id)
            matches = await registered_file_ids(session, owner, slot)
            if len(matches) != 1 or (manifest is not None and matches[0] != manifest):
                raise ServiceError(404, "flow_diagnostic_not_found", "阶段记录已不可用")
            if identifier is not None:
                await require_usage(session, owner, identifier)
            return matches[0]

        async with self.identity.access("flow:diagnose") as (session, _):
            manifest = await check(session)
        detail = await self._read_attempt(manifest)
        if detail.attempt != attempt or index >= len(detail.attachments):
            raise ServiceError(404, "flow_diagnostic_not_found", "此阶段没有对应产物")
        identifier = detail.attachments[index].file_id
        async with self.identity.access("flow:diagnose") as (session, _):
            await check(session, manifest, identifier)
        async with self._open_preview(identifier) as reader:
            yield reader

    @asynccontextmanager
    async def open_output_file(self, run_id, file_id):
        owner = f"flow:{run_id}"
        async with self.identity.access("flow:diagnose") as (session, _):
            await self._require_run(session, run_id)
            await require_slot_usage(session, owner, "output", file_id)
        async with self._open_preview(file_id) as reader:
            yield reader

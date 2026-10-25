"""Bounded recovery of an already generated relay result; never submits a model request."""

import hashlib
import json
from datetime import timedelta

from pydantic import SecretStr

from keystone.core.errors import ServiceError
from keystone.core.time import utcnow
from keystone.platform.model_access.models import Channel, ModelEntry, ModelInvocation
from keystone.platform.model_access.schemas.images import (
    ImageBatch,
    ImageDownloadReceipt,
    ImageManifest,
    ImageOutput,
)


def purpose(identifier, operation, digest):
    return f"image-result:{identifier}:{operation}:{digest}"


def seal_sources(clients, selected, identifier, sources):
    encrypted = clients.credentials.encrypt(
        SecretStr(json.dumps(sources.urls)),
        selected.channel_id,
        purpose(identifier, sources.operation, sources.request_digest),
    )
    return ImageDownloadReceipt(
        operation=sources.operation,
        request_digest=sources.request_digest,
        sources_encrypted=encrypted,
        count=len(sources.urls),
        deadline_at=utcnow() + timedelta(hours=1),
    )


def read_receipt(value):
    try:
        return (
            ImageManifest.model_validate(value).download
            if value.get("kind") == "images"
            else ImageDownloadReceipt.model_validate(value)
        )
    except (ValueError, TypeError, AttributeError):
        raise ServiceError(409, "model_result_unavailable", "原图片取回证据无效") from None


class ImageResultService:
    def __init__(self, identity, clients):
        self.identity, self.clients = identity, clients

    async def fetch(self, identifier, *, selected, context, evidence):
        receipt = evidence.download if isinstance(evidence, ImageManifest) else evidence
        if not isinstance(receipt, ImageDownloadReceipt) or utcnow() >= receipt.deadline_at:
            raise ServiceError(409, "model_result_unavailable", "原图片取回凭据已过期，禁止重发")

        async def authorize():
            async with self.identity.access("model:invoke") as (session, auth):
                row = await session.get(ModelInvocation, identifier)
                entry = await session.get(ModelEntry, selected.model_entry_id)
                channel = await session.get(Channel, selected.channel_id)
                if (
                    row is None
                    or row.status != "succeeded"
                    or row.flow_context
                    != (context.model_dump(mode="json", exclude_none=True) if context else None)
                    or (context is None and row.user_id != auth.user.id)
                    or row.model_entry_id != selected.model_entry_id
                    or row.channel_id != selected.channel_id
                    or row.provider_model != selected.provider_model
                    or row.operation != selected.operation
                    or entry is None
                    or channel is None
                    or not entry.is_active
                    or not channel.is_active
                    or entry.channel_id != selected.channel_id
                    or entry.provider_model != selected.provider_model
                    or selected.operation not in entry.operations
                    or channel.provider != selected.provider
                    or channel.api_base != selected.api_base
                ):
                    raise ServiceError(409, "model_result_unavailable", "原图片配置或归属不匹配")
                current = row.result_context
                expected = read_receipt(current)
                if expected != receipt or utcnow() >= receipt.deadline_at:
                    raise ServiceError(409, "model_result_unavailable", "原图片取回凭据不匹配")

        await authorize()
        try:
            urls = json.loads(
                self.clients.credentials.decrypt(
                    receipt.sources_encrypted,
                    selected.channel_id,
                    purpose(identifier, receipt.operation, receipt.request_digest),
                )
            )
            if not isinstance(urls, list) or len(urls) != receipt.count:
                raise ValueError()
        except (ValueError, TypeError):
            raise ServiceError(409, "model_result_unavailable", "原图片取回凭据无效") from None
        outputs, contents = [], []
        for index, url in enumerate(urls):
            content = await self.clients.inference.download_image(selected, url, authorize)
            mime = await self.clients.inference.verify_output_image(content)
            outputs.append(
                ImageOutput(
                    artifact_key=f"model.{identifier}.{index}",
                    size_bytes=len(content),
                    checksum_sha256=hashlib.sha256(content).hexdigest(),
                    content_type=mime,
                )
            )
            contents.append(content)
        manifest = ImageManifest(
            operation=receipt.operation,
            request_digest=receipt.request_digest,
            outputs=outputs,
            download=receipt,
        )
        async with self.identity.database.transaction() as session:
            row = await session.get(ModelInvocation, identifier, with_for_update=True)
            if row is None or row.status != "succeeded":
                raise ServiceError(409, "model_result_unavailable", "原图片调用状态不匹配")
            current = row.result_context
            if read_receipt(current) != receipt:
                raise ServiceError(409, "model_result_unavailable", "原图片取回凭据不匹配")
            if current.get("kind") == "images":
                original = ImageManifest.model_validate(current)
                if original != manifest:
                    raise ServiceError(409, "model_result_unavailable", "原图片产物发生变化")
            row.result_context = manifest.model_dump(mode="json")
            row.updated_at = utcnow()
        return ImageBatch(manifest, tuple(contents))

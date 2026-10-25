"""Direct, bounded media calls through the existing runtime and public Files service."""

from uuid import uuid4

from keystone.core.errors import ServiceError
from keystone.platform.files.schemas import Upload
from keystone.platform.model_access.schemas.images import (
    MAX_IMAGE_BYTES,
    ImageEditRequest,
    ImageGenerateRequest,
    ImageResult,
    VisionRequest,
)
from keystone.platform.model_access.schemas.invocations import TextResult
from keystone.platform.model_access.services.entries import ModelEntryService
from keystone.platform.model_access.services.runtime import ModelRuntime


class ModelMediaService:
    def __init__(self, identity, clients, files):
        self.identity, self.clients, self.files = identity, clients, files

    async def invoke(self, data, operation):
        entries = ModelEntryService(self.identity, self.clients)
        selected = await entries.selection(data.model_key, operation=operation)
        model = ModelRuntime(entries)
        contract = {
            "image.generate": ImageGenerateRequest,
            "image.edit": ImageEditRequest,
            "image.understand": VisionRequest,
        }[operation]
        request = contract.model_validate(data.model_dump(mode="json", exclude={"model_key"}))
        if operation != "image.understand":
            async with self.identity.access("file:create"):
                pass
        references = getattr(request, "images", ())
        max_images, maximum = model.image_limits(selected)
        if len(references) > max_images:
            raise ServiceError(422, "model_invalid_request", "图片数量超过协议上限")
        images, total = [], 0
        for reference in references:
            async with self.files.open_content(
                reference.file_id, max_bytes=MAX_IMAGE_BYTES
            ) as reader:
                total += reader.size
                if total > maximum:
                    raise ServiceError(413, "model_input_too_large", "图片累计大小超过上限")
                content = bytearray()
                async for chunk in reader.chunks():
                    content.extend(chunk)
                images.append((reader.content_type, bytes(content), reference.detail))
        identifier = uuid4()
        if operation == "image.understand":
            content = await model.invoke_vision(
                request,
                images=images,
                selection=selected,
                invocation_id=identifier,
                context=None,
                worker_identity=None,
            )
            return TextResult(invocation_id=identifier, content=content)
        batch = await model.invoke_image(
            request,
            images=images,
            selection=selected,
            invocation_id=identifier,
            context=None,
            worker_identity=None,
        )
        files = []
        for output, content in zip(batch.manifest.outputs, batch.contents, strict=True):
            extension = output.content_type.removeprefix("image/")
            uploaded = await self.files.upload(
                Upload(
                    name=f"{output.artifact_key}.{extension}",
                    content_type=output.content_type,
                    expected_size=output.size_bytes,
                    checksum_sha256=output.checksum_sha256,
                    request_key=output.artifact_key,
                ),
                content,
            )
            files.append({"file_id": uploaded["id"]})
        return ImageResult(invocation_id=identifier, files=files)

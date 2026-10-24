"""Bounded raster verification and wire encoding; no files, identity or database."""

import base64
import hashlib
import warnings
from io import BytesIO

from PIL import Image

from keystone.core.errors import ServiceError
from keystone.platform.model_access.schemas.images import MAX_IMAGE_BYTES, MAX_IMAGE_PIXELS

MIME = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}


def verify_image(content, content_type, *, max_bytes=MAX_IMAGE_BYTES):
    if not 0 < len(content) <= max_bytes:
        raise ServiceError(413, "model_input_too_large", "图片超过单文件上限")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(content), formats=list(MIME)) as image:
                detected = MIME.get(image.format)
                if (
                    detected is None
                    or content_type is not None
                    and detected != content_type
                    or image.width * image.height > MAX_IMAGE_PIXELS
                    or getattr(image, "n_frames", 1) != 1
                ):
                    raise ValueError()
                image.verify()
            # verify() checks structure; load() also rejects truncated JPEG/WebP payloads.
            with Image.open(BytesIO(content), formats=list(MIME)) as image:
                image.load()
    except Exception:
        raise ServiceError(422, "model_invalid_image", "图片格式、类型或像素范围无效") from None
    return detected


def image_data_uri(content, content_type):
    detected = verify_image(content, content_type)
    return f"data:{detected};base64," + base64.b64encode(content).decode("ascii")


def image_batch(response, data, identifier, digest, operation):
    from keystone.platform.model_access.schemas.images import (
        MAX_OUTPUT_IMAGE_BYTES,
        ImageBatch,
        ImageManifest,
        ImageOutput,
    )

    try:
        items = response["data"]
        if not isinstance(items, list) or not 1 <= len(items) <= data.n:
            raise ValueError()
        outputs, contents = [], []
        content_type = "image/" + data.output_format
        for index, item in enumerate(items):
            encoded = item["b64_json"]
            if (
                not isinstance(encoded, str)
                or len(encoded) > ((MAX_OUTPUT_IMAGE_BYTES + 2) // 3) * 4
            ):
                raise ValueError()
            content = base64.b64decode(encoded, validate=True)
            if item.get("media_type", content_type) != content_type:
                raise ValueError()
            verify_image(content, content_type, max_bytes=MAX_OUTPUT_IMAGE_BYTES)
            contents.append(content)
            outputs.append(
                ImageOutput(
                    artifact_key=f"model.{identifier}.{index}",
                    checksum_sha256=hashlib.sha256(content).hexdigest(),
                    size_bytes=len(content),
                    content_type=content_type,
                )
            )
        return ImageBatch(
            ImageManifest(request_digest=digest, operation=operation, outputs=outputs),
            tuple(contents),
        )
    except ServiceError:
        raise
    except Exception:
        raise ServiceError(502, "model_invalid_output", "图片响应不符合本次操作契约") from None

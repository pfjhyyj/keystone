"""Yunwu/Openlux GPT Image wire protocol; no persistence or implicit POST retries."""

import json
import math
from email.parser import BytesParser
from email.policy import default
from urllib.parse import urlsplit

from keystone.platform.model_access.adapters.inference import images
from keystone.platform.model_access.schemas.errors import AdapterError
from keystone.platform.model_access.schemas.images import (
    MAX_IMAGE_BYTES,
    MAX_RELAY_INPUT_BYTES,
    ImageSources,
)

RATIOS = {"1:1": (1, 1), "16:9": (16, 9), "9:16": (9, 16), "4:3": (4, 3), "3:4": (3, 4)}
MODELS = frozenset(
    {"gpt-image-2", "gpt-image-2-c", "gpt-image-2.5-sunburst", "gpt-image-2.5-flare"}
)


def image_size(ratio, resolution):
    """Exact ratios, multiples of 16, within the GPT Image 2 pixel/edge envelope."""
    w, h = RATIOS[ratio or "1:1"]
    preferred = {"1K": 1024, "2K": 2048, "4K": 3840}[resolution]
    quantum = math.lcm(16 // math.gcd(w, 16), 16 // math.gcd(h, 16))
    candidates = [
        k
        for k in range(quantum, 3841, quantum)
        if 655360 <= w * h * k * k <= 8294400 and max(w, h) * k <= 3840
    ]
    k = min(candidates, key=lambda k: (abs(max(w, h) * k - preferred), -k))
    return f"{w * k}x{h * k}"


def download_host(url):
    try:
        if not isinstance(url, str) or not 1 <= len(url) <= 3072:
            raise ValueError()
        parsed = urlsplit(url)
        host = parsed.hostname
        if (
            parsed.scheme != "https"
            or not host
            or parsed.port not in {None, 443}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or any(c.isspace() or ord(c) < 32 for c in url)
            or "\\" in url
        ):
            raise ValueError()
        return host
    except ValueError:
        raise AdapterError(502, "model_result_source_denied", "图片结果来源无效") from None


def result(response, data, identifier, digest, operation):
    try:
        items = response["data"]
        if not isinstance(items, list) or not 1 <= len(items) <= data.n:
            raise ValueError()
        if all(isinstance(item, dict) and item.get("b64_json") for item in items):
            # Some relays prefix their otherwise standard Base64 result with a data URI.
            normalized = []
            for item in items:
                encoded = item["b64_json"]
                prefix = f"data:image/{data.output_format};base64,"
                if isinstance(encoded, str) and encoded.startswith(prefix):
                    encoded = encoded[len(prefix) :]
                normalized.append({"b64_json": encoded})
            return images.image_batch({"data": normalized}, data, identifier, digest, operation)
        # Every output must have a recoverable URL; never persist Base64 in SQL.
        urls = tuple(item["url"] for item in items)
        for url in urls:
            download_host(url)
        return ImageSources(urls, operation, digest)
    except AdapterError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError):
        raise AdapterError(502, "model_invalid_output", "图片响应缺少有效结果") from None


def validate_request(request, selected, target):
    try:
        if request.method != "POST" or str(request.url) != target[0] + target[1]:
            raise ValueError()
        count, total = 0, 0
        if selected.operation == "image.edit":
            message = BytesParser(policy=default).parsebytes(
                b"Content-Type: "
                + request.headers["content-type"].encode()
                + b"\r\n\r\n"
                + request.content
            )
            if not message.is_multipart() or message.defects:
                raise ValueError()
            payload = {}
            for part in message.iter_parts():
                name = part.get_param("name", header="content-disposition")
                content = part.get_payload(decode=True)
                if part.get_content_disposition() != "form-data" or not isinstance(content, bytes):
                    raise ValueError()
                if name == "image":
                    count += 1
                    total += len(content)
                    if (
                        not 0 < len(content) <= MAX_IMAGE_BYTES
                        or part.get_content_type() not in images.MIME.values()
                    ):
                        raise ValueError()
                else:
                    if name in payload or len(content) > 65536 or part.get_filename() is not None:
                        raise ValueError()
                    payload[name] = content.decode("utf8")
            if not 1 <= count <= 9 or total > MAX_RELAY_INPUT_BYTES:
                raise ValueError()
            if payload.get("n") not in {"1", "2"}:
                raise ValueError()
            payload["n"] = int(payload["n"])
        else:
            payload = json.loads(request.content)
        if (
            set(payload) != {"model", "prompt", "n", "size", "quality", "output_format"}
            or payload["model"] != selected.provider_model
            or not isinstance(payload["prompt"], str)
            or not 1 <= len(payload["prompt"]) <= 16000
            or type(payload["n"]) is not int
            or not 1 <= payload["n"] <= 2
            or payload["quality"] not in {"low", "medium", "high"}
            or payload["output_format"] not in {"png", "jpeg", "webp"}
            or payload["size"] not in {image_size(r, t) for r in RATIOS for t in ("1K", "2K", "4K")}
        ):
            raise ValueError()
    except (KeyError, TypeError, ValueError, AttributeError, UnicodeError):
        raise AdapterError(422, "model_invalid_request", "图片请求不符合受控协议") from None


class OpenAIImages:
    image_parameters = ("aspect_ratio", "quality")
    operations = frozenset({"image.generate", "image.edit"})
    delivery = "sync"
    models = MODELS
    paths = {"/images/generations": "POST", "/images/edits": "POST"}
    require_parameters = False
    timeout_seconds = 600
    max_input_bytes = MAX_RELAY_INPUT_BYTES
    max_images = 9

    def request_target(self, selected):
        path = "/images/edits" if selected.operation == "image.edit" else "/images/generations"
        return selected.api_base, path, "model"

    validate_request = staticmethod(validate_request)

    async def invoke(self, sdk, selected, data, inputs, identifier, digest, run_sync):
        body = dict(
            model=selected.provider_model,
            prompt=data.prompt,
            n=data.n,
            size=image_size(data.aspect_ratio, data.resolution),
            quality=data.quality or "medium",
            output_format=data.output_format,
        )
        files = None
        if selected.operation == "image.edit":
            files = []
            for index, (mime, content, _) in enumerate(inputs):
                await run_sync(images.verify_image, content, mime)
                files.append(("image", (f"reference-{index}", content, mime)))
        response = await sdk.post(
            self.request_target(selected)[1],
            body=body,
            files=files,
            cast_to=dict,
            options={"headers": {"Content-Type": "multipart/form-data"}} if files else {},
        )
        return await run_sync(result, response, data, identifier, digest, selected.operation)

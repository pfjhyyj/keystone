"""Actual SDK/multipart send boundary for the shared Yunwu/Openlux image protocol."""

import base64
import hashlib
import json
from email.parser import BytesParser
from email.policy import default
from uuid import uuid4

import httpx2
import pytest
from support.model_stub import image_bytes

from keystone.core.errors import ServiceError
from keystone.platform.model_access.adapters.http import HttpClient
from keystone.platform.model_access.adapters.inference.client import InferenceClient
from keystone.platform.model_access.adapters.inference.transport import Observation
from keystone.platform.model_access.channels import CHANNELS, channel_adapter
from keystone.platform.model_access.channels.shared.openai_images import (
    download_host,
    image_size,
)
from keystone.platform.model_access.schemas.errors import AdapterError
from keystone.platform.model_access.schemas.images import (
    ImageBatch,
    ImageEditRequest,
    ImageGenerateRequest,
    ImageSources,
)
from keystone.platform.model_access.schemas.invocations import Selection
from keystone.platform.model_access.services.runtime import ModelRuntime


def selected(provider, operation="image.generate"):
    return Selection(
        model_entry_id=uuid4(),
        channel_id=uuid4(),
        provider=provider,
        provider_model="gpt-image-2",
        api_base=CHANNELS[provider].api_base,
        operation=operation,
    )


@pytest.mark.parametrize(
    "provider,edit,kind",
    [
        ("yunwu", False, "base64"),
        ("yunwu", False, "url"),
        ("yunwu", True, "base64"),
        ("yunwu", True, "url"),
        ("openlux", True, "url"),
        ("openai", False, "base64"),
        ("openai", True, "base64"),
    ],
)
async def test_image_wire_preserves_registration_receipt_and_nine_reference_files(
    monkeypatch, provider, edit, kind
):
    calls, registered = [], []
    selection = selected(provider, "image.edit" if edit else "image.generate")
    data = (
        ImageEditRequest(
            prompt="combine",
            images=[{"file_id": uuid4()} for _ in range(9)],
            aspect_ratio="3:4",
            resolution="2K",
            quality="high",
        )
        if edit
        else ImageGenerateRequest(prompt="draw")
    )
    raw = image_bytes()
    # Official edits repeat image[]; the relays repeat image. Neither accepts the other.
    field = "image[]" if provider == "openai" else "image"
    receipt_header = "x-request-id" if provider == "openai" else "x-api-request-id"

    async def register():
        registered.append(True)
        return "private-secret"

    async def receipt():
        assert registered

    def respond(request):
        assert registered == [True]
        assert request.headers["authorization"] == "Bearer private-secret"
        # A slow 4K reply must not expire before the protocol's own 1200 s deadline.
        assert request.extensions["timeout"]["read"] == 1200
        if edit:
            message = BytesParser(policy=default).parsebytes(
                b"Content-Type: "
                + request.headers["content-type"].encode()
                + b"\r\n\r\n"
                + request.content
            )
            fields, files = {}, []
            for part in message.iter_parts():
                name = part.get_param("name", header="content-disposition")
                if name == field:
                    files.append(part.get_payload(decode=True))
                else:
                    fields[name] = part.get_payload(decode=True).decode()
            assert files == [raw] * 9 and "image" not in fields and "image[]" not in fields
            assert fields["quality"] == "high" and fields["size"] == "1536x2048"
            assert request.url.path == "/v1/images/edits"
        else:
            fields = json.loads(request.content)
            assert fields["quality"] == "medium" and fields["size"] == "1024x1024"
            assert request.url.path == "/v1/images/generations"
        calls.append(fields)
        item = (
            {"b64_json": base64.b64encode(raw).decode()}
            if kind == "base64"
            else {"url": "https://cdn.example.test/output.png?signature=private"}
        )
        return httpx2.Response(
            200, headers={receipt_header: "bill-original"}, json={"created": 1, "data": [item]}
        )

    http = HttpClient()
    monkeypatch.setattr(http, "transport", lambda _: httpx2.MockTransport(respond))
    client = InferenceClient(http)
    observation = Observation(lambda _: None)
    inputs = [("image/png", raw, "auto")] * (9 if edit else 0)
    identifier = uuid4()

    async def send(sdk):
        return await client.image(sdk, selection, data, inputs, identifier, "a" * 64)

    events = [
        event
        async for event in client.events(
            provider, selection, observation, register, receipt, identifier, sdk_request=send
        )
    ]
    result = events[0]["content"]
    assert isinstance(result, ImageBatch if kind == "base64" else ImageSources)
    assert len(calls) == 1 and observation.receipt_ids[receipt_header] == "bill-original"


def test_exact_sizes_and_image_request_digest_are_stable():
    for ratio in ("1:1", "16:9", "9:16", "4:3", "3:4"):
        a, b = map(int, ratio.split(":"))
        for tier in ("1K", "2K", "4K"):
            w, h = map(int, image_size(ratio, tier).split("x"))
            assert w * b == h * a and w % 16 == h % 16 == 0
            assert 655360 <= w * h <= 8294400 and max(w, h) <= 3840
    data = ImageGenerateRequest(prompt="old")
    original = {
        "prompt": "old",
        "n": 1,
        "resolution": "1K",
        "output_format": "png",
        "aspect_ratio": None,
        "quality": None,
    }
    raw = json.dumps(
        ["image.generate", original],
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    assert ModelRuntime.image_digest(data, "image.generate") == hashlib.sha256(raw).hexdigest()
    changed = data.model_copy(update={"quality": "high"})
    assert ModelRuntime.image_digest(changed, "image.generate") != hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize(
    "url",
    [
        "http://cdn.test/x",
        "https://user:pass@cdn.test/x",
        "https://cdn.test:8443/x",
        "https://cdn.test/x#fragment",
        "https://cdn.test/\nx",
    ],
)
def test_result_source_rejects_credentials_and_unsafe_urls(url):
    with pytest.raises(AdapterError):
        download_host(url)


async def test_download_retries_only_get_reauthorizes_and_never_sends_a_token(monkeypatch):
    http = HttpClient()
    checks, calls = [], []

    async def authorize():
        checks.append(True)

    def respond(request):
        calls.append(request)
        assert request.method == "GET" and request.url.host == "cdn.example.test"
        assert "authorization" not in request.headers and "key" not in request.url.params
        assert request.headers["host"] == "cdn.example.test"
        return (
            httpx2.Response(503) if len(calls) == 1 else httpx2.Response(200, content=image_bytes())
        )

    monkeypatch.setattr(http, "transport", lambda _: httpx2.MockTransport(respond))
    result = await InferenceClient(http).download_image(
        selected("yunwu"), "https://cdn.example.test/output.png?signature=private", authorize
    )
    assert result == image_bytes() and len(calls) == len(checks) == 2


async def test_multipart_validation_precedes_registration(monkeypatch):
    http = HttpClient()

    def unexpected(request):
        pytest.fail("An invalid request reached the upstream")

    monkeypatch.setattr(http, "transport", lambda _: httpx2.MockTransport(unexpected))
    client, selection = InferenceClient(http), selected("yunwu", "image.edit")

    async def register():
        pytest.fail("An invalid request was registered")

    async def send(sdk):
        return await sdk.post(
            "/images/edits",
            cast_to=dict,
            body={"model": "gpt-image-2", "n": 99},
            files=[("image", ("ref.png", image_bytes(), "image/png"))],
            options={"headers": {"Content-Type": "multipart/form-data"}},
        )

    with pytest.raises(Exception) as caught:
        async for _ in client.events(
            "yunwu",
            selection,
            Observation(lambda _: None),
            register,
            register,
            uuid4(),
            sdk_request=send,
        ):
            pass
    from keystone.platform.model_access.adapters.inference.errors import classify_error

    assert classify_error(caught.value)[1] == "model_invalid_request"


async def test_maximum_reference_bytes_and_multibyte_prompt_fit_the_wire_limit(monkeypatch):
    http, sent, registered = HttpClient(), [], []
    selection = selected("yunwu", "image.edit")
    data = ImageEditRequest(prompt="😀" * 16000, images=[{"file_id": uuid4()} for _ in range(8)])
    raw = image_bytes().ljust(4 * 1024 * 1024, b"\0")

    async def register():
        registered.append(True)
        return "private-secret"

    async def receipt():
        pass

    def respond(request):
        assert registered == [True]
        # Includes UTF-8 fields, all file headers and multipart boundaries.
        assert 32 * 1024 * 1024 + 65536 < len(request.content) <= 32 * 1024 * 1024 + 128 * 1024
        sent.append(True)
        return httpx2.Response(200, json={"data": [{"url": "https://cdn.example.test/result"}]})

    monkeypatch.setattr(http, "transport", lambda _: httpx2.MockTransport(respond))
    client = InferenceClient(http)
    identifier = uuid4()

    async def send(sdk):
        return await client.image(
            sdk, selection, data, [("image/png", raw, "auto")] * 8, identifier, "a" * 64
        )

    events = [
        event
        async for event in client.events(
            "yunwu",
            selection,
            Observation(lambda _: None),
            register,
            receipt,
            identifier,
            sdk_request=send,
        )
    ]
    assert sent == [True] and isinstance(events[0]["content"], ImageSources)


@pytest.mark.parametrize(
    "destination", ["https://next.example.test/result.png", "https://127.0.0.1/private"]
)
async def test_download_redirects_preserve_destination_and_never_forward_credentials(
    monkeypatch, destination
):
    http, calls = HttpClient(), []

    async def authorize():
        pass

    def respond(request):
        calls.append(request)
        assert "authorization" not in request.headers
        if request.url.host == "cdn.example.test":
            return httpx2.Response(302, headers={"location": destination})
        assert str(request.url) == destination
        return httpx2.Response(200, content=image_bytes())

    monkeypatch.setattr(http, "transport", lambda _: httpx2.MockTransport(respond))
    assert (
        await InferenceClient(http).download_image(
            selected("openlux"), "https://cdn.example.test/result.png?signature=private", authorize
        )
        == image_bytes()
    )
    assert len(calls) == 2


@pytest.mark.parametrize(
    "provider,accepted,rejected",
    [("openai", "image[]", "image"), ("yunwu", "image", "image[]")],
)
def test_reference_field_name_is_fixed_per_site_for_validation_and_pricing(
    provider, accepted, rejected
):
    from keystone.platform.model_access.adapters.inference.pricing import (
        profile_for,
        request_context,
    )

    selection = selected(provider, "image.edit")
    protocol = CHANNELS[provider].protocol("image.edit", "sync", model="gpt-image-2")
    target = protocol.request_target(selection)
    fields = {
        "model": "gpt-image-2",
        "prompt": "combine",
        "n": "1",
        "size": "1024x1024",
        "quality": "medium",
        "output_format": "png",
    }

    def multipart(name):
        request = httpx2.Request(
            "POST",
            target[0] + target[1],
            data=fields,
            files=[(name, (f"reference-{i}", image_bytes(), "image/png")) for i in range(3)],
        )
        request.read()
        return request

    request = multipart(accepted)
    protocol.validate_request(request, selection, target)
    context = request_context(request, profile_for(protocol))
    assert context["reference_images"] == 3 and context["size"] == "1024x1024"
    with pytest.raises(AdapterError) as error:
        protocol.validate_request(multipart(rejected), selection, target)
    assert error.value.code == "model_invalid_request"


def test_official_and_relay_image_protocols_share_limits_but_not_models():
    official = CHANNELS["openai"].protocol("image.generate", "sync", model="gpt-image-2")
    relay = CHANNELS["yunwu"].protocol("image.generate", "sync", model="gpt-image-2")
    for protocol in (official, relay):
        assert protocol.timeout_seconds == 1200
        assert (protocol.max_images, protocol.max_input_bytes) == (9, 32 * 1024 * 1024)
    assert "gpt-image-2-c" in relay.models and "gpt-image-2-c" not in official.models
    assert "gpt-image-2.5-flare-2026-09-08" in official.models
    assert CHANNELS["openai"].custom_base is False
    assert channel_adapter("openai", "https://proxy.example.test/v1") is CHANNELS["openai"]
    with pytest.raises(ServiceError) as error:
        InferenceClient(None).check_target(
            "openai", "https://proxy.example.test/v1", "/images/generations"
        )
    assert error.value.code == "model_target_not_allowed"


def test_largest_protocol_size_png_fits_output_and_response_bounds():
    """Noise RGB at the largest generated size: far above the old 8 MiB, within 32 MiB."""
    import random
    from io import BytesIO

    from PIL import Image

    from keystone.platform.model_access.channels.shared.images import image_batch
    from keystone.platform.model_access.schemas.images import (
        MAX_IMAGE_RESPONSE_BYTES,
        MAX_OUTPUT_IMAGE_BYTES,
    )

    width, height = map(int, image_size("16:9", "4K").split("x"))
    assert (width, height) == (3840, 2160)
    noise = random.Random(0).randbytes(width * height * 3)
    output = BytesIO()
    Image.frombytes("RGB", (width, height), noise).save(output, "PNG", compress_level=1)
    raw = output.getvalue()
    assert 8 * 1024 * 1024 < len(raw) <= MAX_OUTPUT_IMAGE_BYTES
    encoded = base64.b64encode(raw).decode()
    body = json.dumps({"created": 1, "data": [{"b64_json": encoded}] * 2})
    assert len(body) <= MAX_IMAGE_RESPONSE_BYTES
    data = ImageGenerateRequest(prompt="draw", n=2, resolution="4K", aspect_ratio="16:9")
    batch = image_batch(json.loads(body), data, uuid4(), "a" * 64, "image.generate")
    assert [output.size_bytes for output in batch.manifest.outputs] == [len(raw)] * 2
    # Base64 beyond the per-output bound is rejected before decoding.
    oversized = "A" * (((MAX_OUTPUT_IMAGE_BYTES + 2) // 3) * 4 + 4)
    with pytest.raises(AdapterError) as error:
        image_batch({"data": [{"b64_json": oversized}]}, data, uuid4(), "a" * 64, "image.generate")
    assert error.value.code == "model_invalid_output"

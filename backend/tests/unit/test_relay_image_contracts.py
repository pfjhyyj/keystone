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

from keystone.platform.model_access.adapters.channels import CHANNELS
from keystone.platform.model_access.adapters.http import HttpClient
from keystone.platform.model_access.adapters.inference.client import InferenceClient
from keystone.platform.model_access.adapters.inference.openai_images import (
    download_host,
    image_size,
)
from keystone.platform.model_access.adapters.inference.transport import Observation
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


@pytest.mark.parametrize("provider", ["yunwu", "openlux"])
@pytest.mark.parametrize("edit", [False, True])
@pytest.mark.parametrize("kind", ["base64", "url"])
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

    async def register():
        registered.append(True)
        return "private-secret"

    async def receipt():
        assert registered

    def respond(request):
        assert registered == [True]
        assert request.headers["authorization"] == "Bearer private-secret"
        assert request.extensions["timeout"]["read"] == 600
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
                if name == "image":
                    files.append(part.get_payload(decode=True))
                else:
                    fields[name] = part.get_payload(decode=True).decode()
            assert files == [raw] * 9
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
            200, headers={"x-api-request-id": "bill-original"}, json={"created": 1, "data": [item]}
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
    assert len(calls) == 1 and observation.receipt_ids["x-api-request-id"] == "bill-original"
    assert observation.request_budget.operation == selection.operation


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

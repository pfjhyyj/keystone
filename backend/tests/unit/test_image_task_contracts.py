"""Native task and download contracts reject unsafe targets before network I/O."""

from datetime import timedelta
from types import SimpleNamespace

import httpx2
import pytest
from pydantic import ValidationError

from keystone.core.time import utcnow
from keystone.platform.flow.schemas.definitions import ModelRole
from keystone.platform.model_access.adapters.http import HttpClient, ResponseTooLarge
from keystone.platform.model_access.adapters.inference import kling
from keystone.platform.model_access.schemas.errors import AdapterError, QueryLater
from keystone.platform.model_access.schemas.images import ImageTaskReceipt


def receipt(**changes):
    now = utcnow()
    return ImageTaskReceipt(
        **{
            "task_id": "original-task",
            "request_digest": "a" * 64,
            "count": 1,
            "submitted_at": now,
            "deadline_at": now + timedelta(seconds=300),
            "next_query_at": now,
            **changes,
        }
    )


def test_task_receipts_keep_original_identity_and_bounded_query_window():
    item = receipt()
    assert ImageTaskReceipt.model_validate_json(item.model_dump_json()) == item
    for changes in (
        {"queries": 61},
        {"task_id": "../other-task"},
        {"count": 0},
        {"deadline_at": item.submitted_at + timedelta(seconds=301)},
        {"next_query_at": item.submitted_at - timedelta(seconds=1)},
    ):
        with pytest.raises(ValidationError):
            ImageTaskReceipt.model_validate({**item.model_dump(), **changes})
    for operation in ("text.generate", "image.edit"):
        with pytest.raises(ValueError):
            ModelRole("role", "Role", operation, delivery="async")
    ModelRole("role", "Role", "image.generate", delivery="async")


def test_only_the_original_task_and_supported_https_sources_are_accepted():
    item = receipt()
    for state in ("submitted", "processing", "failed"):
        assert kling.status(
            {"code": 0, "data": {"task_id": item.task_id, "task_status": state}}, item
        ) == (state, ())
    valid = {
        "code": 0,
        "data": {
            "task_id": item.task_id,
            "task_status": "succeed",
            "task_result": {
                "images": [{"url": "https://p2-fdl.klingai.com/result.png?signature=test"}]
            },
        },
    }
    assert kling.status(valid, item)[0] == "succeed"
    for changes in (
        {"task_id": "other-task"},
        {"task_status": "unknown"},
        {"status": "failed"},
        {"task_result": {"images": []}},
    ):
        with pytest.raises(AdapterError):
            kling.status({**valid, "data": {**valid["data"], **changes}}, item)
    for url in (
        "http://p2-fdl.klingai.com/file.png",
        "https://127.0.0.1/file.png",
        "https://p2-fdl.klingai.com.evil.invalid/file.png",
        "https://user:secret@p2-fdl.klingai.com/file.png",
        "https://p2-fdl.klingai.com:8443/file.png",
        "https://p2-fdl.klingai.com/file.png#fragment",
    ):
        with pytest.raises(AdapterError) as error:
            kling.download_host(url)
        assert error.value.code == "model_result_source_denied"
    selected = SimpleNamespace(provider="openlux", api_base=kling.BASE, provider_model="not-kling")
    with pytest.raises(AdapterError):
        kling.submit_body(selected, SimpleNamespace(prompt="x", n=1, resolution="1K"))


@pytest.mark.parametrize("header,seconds", [("7", 7), ("999999", 60), ("²", 5), ("9" * 5000, 5)])
def test_retry_after_is_bounded_even_for_untrusted_numeric_header(header, seconds):
    with pytest.raises(QueryLater) as error:
        kling.check_query(httpx2.Response(429, headers={b"retry-after": header.encode()}))
    assert error.value.seconds == seconds


async def test_download_preserves_hostname_and_has_no_credentials(monkeypatch):
    calls = []

    async def send(request):
        calls.append(request)
        return httpx2.Response(200, content=b"bounded")

    http = HttpClient()
    monkeypatch.setattr(http, "transport", lambda provider: httpx2.MockTransport(send))
    result = await http.get_bytes(
        "openlux",
        "https://p2-fdl.klingai.com/result.png?signature=private",
        hostname="p2-fdl.klingai.com",
        limit=7,
        check_status=kling.check_download,
    )
    assert result == b"bounded" and len(calls) == 1
    assert calls[0].url.host == "p2-fdl.klingai.com"
    assert "authorization" not in calls[0].headers
    with pytest.raises(ResponseTooLarge):
        await http.get_bytes(
            "openlux",
            "https://p2-fdl.klingai.com/result.png",
            hostname="p2-fdl.klingai.com",
            limit=6,
            check_status=kling.check_download,
        )


@pytest.mark.parametrize("host", ["127.0.0.1", "10.0.0.1", "198.18.1.236", "::1"])
async def test_download_accepts_configured_private_and_fake_ip_hosts(monkeypatch, host):
    calls = []

    def respond(request):
        calls.append(request)
        assert request.url.host == host
        assert "authorization" not in request.headers
        return httpx2.Response(200, content=b"bounded")

    http = HttpClient()
    monkeypatch.setattr(http, "transport", lambda _: httpx2.MockTransport(respond))
    netloc = f"[{host}]" if ":" in host else host
    assert (
        await http.get_bytes(
            "openlux",
            f"https://{netloc}/result.png",
            hostname=host,
            limit=7,
            check_status=kling.check_download,
        )
        == b"bounded"
    )
    assert len(calls) == 1

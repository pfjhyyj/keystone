import asyncio
import base64
import hashlib
import hmac
import json

import httpx2
import pytest

from keystone.platform.messaging.adapters import feishu_webhook

URL = "https://open.feishu.cn/open-apis/bot/v2/hook/test-token"


def run(handler, content, secret=None):
    transport = httpx2.MockTransport(handler)
    return asyncio.run(feishu_webhook.send(URL, secret, content, transport=transport))


def test_signed_text_and_card_bodies():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx2.Response(200, json={"code": 0, "msg": "success", "data": {}})

    run(handler, {"kind": "text", "text": "你好"}, secret="s3cret")
    body = seen[0]
    key = f"{body['timestamp']}\ns3cret".encode()
    expected = base64.b64encode(hmac.new(key, b"", hashlib.sha256).digest()).decode()
    assert body["sign"] == expected
    assert body["msg_type"] == "text" and body["content"] == {"text": "你好"}

    card = {
        "kind": "card",
        "title": "日报",
        "body": '<at user_id="all">不会被解析</at>',
        "fields": [{"label": "状态", "value": "完成"}],
        "links": [{"text": "查看", "url": "https://example.com/r/1"}],
    }
    run(handler, card)
    sent = seen[1]
    assert sent["msg_type"] == "interactive" and "sign" not in sent
    tags = [element["tag"] for element in sent["card"]["elements"]]
    assert tags == ["div", "div", "action"]
    # User text is plain text, so mentions or markdown in content are not interpreted.
    assert sent["card"]["elements"][0]["text"]["tag"] == "plain_text"
    assert sent["card"]["elements"][2]["actions"][0]["url"] == "https://example.com/r/1"


@pytest.mark.parametrize(
    ("response", "outcome"),
    [
        (httpx2.Response(200, json={"code": 19021, "msg": "sign match fail"}), "feishu_19021"),
        (
            httpx2.Response(200, json={"code": 11232, "msg": "frequency limited"}),
            "provider_rate_limited",
        ),
        (httpx2.Response(400, text="bad request"), "provider_rejected"),
        (httpx2.Response(502, text="bad gateway"), None),
        (httpx2.Response(200, text="not json"), None),
    ],
)
def test_outcomes_split_rejected_from_unknown(response, outcome):
    expected = feishu_webhook.Rejected if outcome else feishu_webhook.Unknown
    with pytest.raises(expected) as error:
        run(lambda request: response, {"kind": "text", "text": "x"})
    if outcome:
        assert error.value.code == outcome


def test_transport_failure_is_unknown_and_oversize_is_rejected():
    def broken(request):
        raise httpx2.ConnectError("down")

    with pytest.raises(feishu_webhook.Unknown):
        run(broken, {"kind": "text", "text": "x"})
    with pytest.raises(feishu_webhook.Rejected) as error:
        run(broken, {"kind": "text", "text": "长" * 8000})
    assert error.value.code == "content_too_large"

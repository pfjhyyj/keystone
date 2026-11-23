import asyncio
import json

import httpx2
import pytest

from keystone.platform.messaging.adapters import feishu_app

APP, SECRET = "cli_a1b2c3", "app-secret-value"
CHAT = "oc_0123456789"


@pytest.fixture(autouse=True)
def fresh_tokens():
    feishu_app._tokens.clear()
    yield
    feishu_app._tokens.clear()


class Provider:
    """Answers the token endpoint and records Open API calls."""

    def __init__(self, *answers, token_answer=None):
        self.answers, self.token_answer = list(answers), token_answer
        self.tokens, self.calls = 0, []

    def __call__(self, request):
        if request.url.host == "accounts.feishu.cn":
            self.tokens += 1
            form = dict(item.split("=") for item in request.content.decode().split("&"))
            assert form == {
                "grant_type": "client_credentials",
                "client_id": APP,
                "client_secret": SECRET,
            }
            return self.token_answer or httpx2.Response(
                200,
                json={
                    "code": 0,
                    "access_token": f"t-{self.tokens}",
                    "expires_in": 7200,
                    "token_type": "Bearer",
                },
            )
        self.calls.append(request)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def send(provider, content=None):
    return asyncio.run(
        feishu_app.send(
            APP,
            SECRET,
            "chat_id",
            CHAT,
            content or {"kind": "text", "text": "你好"},
            "delivery-id",
            transport=httpx2.MockTransport(provider),
        )
    )


def ok(message_id="om_1"):
    return httpx2.Response(
        200, json={"code": 0, "msg": "success", "data": {"message_id": message_id}}
    )


def test_sends_with_uuid_and_reuses_the_cached_token():
    provider = Provider(ok("om_1"), ok("om_2"))
    assert send(provider) == "om_1"
    card = {"kind": "card", "title": "日报", "fields": [{"label": "状态", "value": "完成"}]}
    assert send(provider, card) == "om_2"
    assert provider.tokens == 1
    first, second = provider.calls
    assert first.url.params["receive_id_type"] == "chat_id"
    assert first.headers["Authorization"] == "Bearer t-1"
    body = json.loads(first.content)
    assert body["uuid"] == "delivery-id" and body["receive_id"] == CHAT
    # Content travels as a serialized JSON string.
    assert body["msg_type"] == "text" and json.loads(body["content"]) == {"text": "你好"}
    sent = json.loads(json.loads(second.content)["content"])
    assert json.loads(second.content)["msg_type"] == "interactive"
    assert sent["header"]["title"]["content"] == "日报"


def test_a_token_the_platform_no_longer_accepts_is_refreshed_once():
    stale = httpx2.Response(400, json={"code": 99991663, "msg": "Invalid access token"})
    provider = Provider(stale, ok())
    assert send(provider) == "om_1"
    assert provider.tokens == 2
    assert provider.calls[1].headers["Authorization"] == "Bearer t-2"

    provider = Provider(stale, stale)
    feishu_app._tokens.clear()
    with pytest.raises(feishu_app.NotSent) as error:
        send(provider)
    assert error.value.code == "credential_invalid"


@pytest.mark.parametrize(
    ("answer", "outcome", "code"),
    [
        (
            httpx2.Response(400, json={"code": 230002, "msg": "bot not in chat"}),
            "rejected",
            "feishu_230002",
        ),
        (
            httpx2.Response(400, json={"code": 230020, "msg": "frequency"}),
            "not_sent",
            "provider_rate_limited",
        ),
        (httpx2.Response(502, text="bad gateway"), "unknown", None),
        (httpx2.Response(200, text="not json"), "unknown", None),
        (httpx2.Response(200, json={"code": 0, "data": {}}), "unknown", None),
        (httpx2.ConnectError("down"), "unknown", None),
    ],
)
def test_outcomes_split_rejected_not_sent_and_unknown(answer, outcome, code):
    expected = {
        "rejected": feishu_app.Rejected,
        "not_sent": feishu_app.NotSent,
        "unknown": feishu_app.Unknown,
    }[outcome]
    with pytest.raises(expected) as error:
        send(Provider(answer))
    if code:
        assert error.value.code == code


def test_token_failures_never_reach_the_message_api():
    provider = Provider(token_answer=httpx2.Response(400, json={"code": 20002, "msg": "secret"}))
    with pytest.raises(feishu_app.Rejected) as error:
        send(provider)
    assert error.value.code == "feishu_token_20002" and provider.calls == []

    def down(request):
        raise httpx2.ConnectError("down")

    with pytest.raises(feishu_app.NotSent) as error:
        asyncio.run(
            feishu_app.send(
                APP,
                SECRET,
                "chat_id",
                CHAT,
                {"kind": "text", "text": "x"},
                "id",
                transport=httpx2.MockTransport(down),
            )
        )
    assert error.value.code == "token_unavailable"


def test_oversized_cards_are_rejected_before_any_call():
    provider = Provider()
    card = {"kind": "card", "title": "t", "body": "长" * 12000}
    with pytest.raises(feishu_app.Rejected) as error:
        send(provider, card)
    assert error.value.code == "content_too_large" and provider.tokens == 0


def test_chats_lists_live_groups_and_pages():
    page = httpx2.Response(
        200,
        json={
            "code": 0,
            "data": {
                "items": [
                    {
                        "chat_id": "oc_a",
                        "name": "运维群",
                        "external": False,
                        "chat_status": "normal",
                    },
                    {"chat_id": "oc_b", "name": "旧群", "chat_status": "dissolved"},
                    {"chat_id": "oc_c", "name": "外部群", "external": True},
                ],
                "has_more": True,
                "page_token": "next",
            },
        },
    )
    provider = Provider(page)
    result = asyncio.run(
        feishu_app.chats(APP, SECRET, "first", transport=httpx2.MockTransport(provider))
    )
    assert result == {
        "items": [
            {"chat_id": "oc_a", "name": "运维群", "external": False},
            {"chat_id": "oc_c", "name": "外部群", "external": True},
        ],
        "page_token": "next",
    }
    params = provider.calls[0].url.params
    assert params["page_size"] == "100" and params["page_token"] == "first"

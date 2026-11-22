"""Feishu group custom bot: push only, no de-duplication, so a send is never retried.

https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot
"""

import asyncio
import base64
import hashlib
import hmac
import json
import time

import httpx2

MAX_BODY = 20 * 1024
MAX_RESPONSE = 64 * 1024
RATE_LIMITED = {11232, 9499}


class Rejected(Exception):
    """The provider answered and did not deliver; safe to report as failed."""

    def __init__(self, code):
        self.code = code


class Unknown(Exception):
    """No trustworthy answer; the message may or may not have been delivered."""


def sign(secret, timestamp):
    # Feishu signs an empty message with key `timestamp + "\n" + secret`.
    key = f"{timestamp}\n{secret}".encode()
    return base64.b64encode(hmac.new(key, b"", hashlib.sha256).digest()).decode()


def render(content):
    if content["kind"] == "text":
        return {"msg_type": "text", "content": {"text": content["text"]}}
    elements = []
    if content.get("body"):
        elements.append({"tag": "div", "text": {"tag": "plain_text", "content": content["body"]}})
    if content.get("fields"):
        elements.append(
            {
                "tag": "div",
                "fields": [
                    {
                        "is_short": True,
                        "text": {
                            "tag": "plain_text",
                            "content": f"{field['label']}：{field['value']}",
                        },
                    }
                    for field in content["fields"]
                ],
            }
        )
    if content.get("links"):
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": link["text"]},
                        "type": "default",
                        "url": link["url"],
                    }
                    for link in content["links"]
                ],
            }
        )
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": content["title"]}},
            "elements": elements,
        },
    }


async def send(url, secret, content, *, transport=None):
    body = render(content)
    if secret:
        timestamp = str(int(time.time()))
        body |= {"timestamp": timestamp, "sign": sign(secret, timestamp)}
    payload = json.dumps(body, ensure_ascii=False).encode()
    if len(payload) > MAX_BODY:
        raise Rejected("content_too_large")
    try:
        async with (
            asyncio.timeout(10),
            httpx2.AsyncClient(
                transport=transport or httpx2.AsyncHTTPTransport(retries=0, trust_env=False),
                timeout=10,
                follow_redirects=False,
                trust_env=False,
            ) as client,
        ):
            async with client.stream(
                "POST", url, content=payload, headers={"Content-Type": "application/json"}
            ) as response:
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_RESPONSE:
                        raise Unknown
                status = response.status_code
    except (httpx2.HTTPError, OSError, TimeoutError):
        raise Unknown from None
    if status >= 500:
        raise Unknown
    try:
        answer = json.loads(data)
        code = answer.get("code", answer.get("StatusCode"))
    except (ValueError, AttributeError):
        raise (Unknown if status < 400 else Rejected("provider_rejected")) from None
    if status < 400 and code == 0:
        return
    if type(code) is not int:
        raise Rejected("provider_rejected")
    if code in RATE_LIMITED:
        raise Rejected("provider_rate_limited")
    raise Rejected(f"feishu_{code}")

"""Feishu group custom bot: push only, no de-duplication, so a send is never retried.

https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot
"""

import base64
import hashlib
import hmac
import json
import time

from keystone.platform.messaging.adapters import feishu_content
from keystone.platform.messaging.adapters.feishu_http import Rejected, Unknown, call

__all__ = ["Rejected", "Unknown", "render", "send", "sign"]

MAX_BODY = 20 * 1024
RATE_LIMITED = {11232, 9499}


def sign(secret, timestamp):
    # Feishu signs an empty message with key `timestamp + "\n" + secret`.
    key = f"{timestamp}\n{secret}".encode()
    return base64.b64encode(hmac.new(key, b"", hashlib.sha256).digest()).decode()


def render(content):
    msg_type, body = feishu_content.render(content)
    # Group bots take the card object itself under `card`, not a serialized `content`.
    return {"msg_type": msg_type, "card" if msg_type == "interactive" else "content": body}


async def send(url, secret, content, *, transport=None):
    body = render(content)
    if secret:
        timestamp = str(int(time.time()))
        body |= {"timestamp": timestamp, "sign": sign(secret, timestamp)}
    payload = json.dumps(body, ensure_ascii=False).encode()
    if len(payload) > MAX_BODY:
        raise Rejected("content_too_large")
    status, answer = await call(
        "POST",
        url,
        transport=transport,
        content=payload,
        headers={"Content-Type": "application/json"},
    )
    if status >= 500:
        raise Unknown
    if answer is None:
        raise Unknown if status < 400 else Rejected("provider_rejected")
    code = answer.get("code", answer.get("StatusCode"))
    if status < 400 and code == 0:
        return
    if type(code) is not int:
        raise Rejected("provider_rejected")
    if code in RATE_LIMITED:
        raise Rejected("provider_rate_limited")
    raise Rejected(f"feishu_{code}")

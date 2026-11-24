"""Feishu self-built app: tenant token, sending with uuid de-duplication, listing bot chats.

https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/authentication-management/access-token/tenant_access_token
https://open.feishu.cn/document/server-docs/im-v1/message/create
https://open.feishu.cn/document/server-docs/im-v1/message/reply
https://open.feishu.cn/document/server-docs/group/chat/list
https://open.feishu.cn/document/server-docs/im-v1/message-card/patch
"""

import hashlib
import json
import time

from keystone.platform.messaging.adapters import feishu_content
from keystone.platform.messaging.adapters.feishu_http import Rejected, Unknown, call

__all__ = [
    "NotSent",
    "Rejected",
    "Unknown",
    "chats",
    "open_id",
    "reply",
    "send",
    "update_card",
    "user_name",
]

TOKEN_URL = "https://accounts.feishu.cn/oauth/v3/token"
API = "https://open.feishu.cn/open-apis"
# The same uuid succeeds at most once within an hour; retries stay well inside that.
DEDUPE_WINDOW_SECONDS = 50 * 60
MAX_BODY = {"text": 150 * 1024, "interactive": 30 * 1024}
TOKEN_INVALID = {99991661, 99991663, 99991665}
RATE_LIMITED = {230020, 99991400, 11232, 11233, 11247}
# Refresh ahead of expiry so a token never lapses mid-request.
TOKEN_MARGIN = 300

# Process memory only: (app_id, secret digest) -> (token, monotonic expiry).
_tokens: dict[tuple[str, str], tuple[str, float]] = {}


class NotSent(Exception):
    """The request was refused before any message existed; sending again is safe."""

    def __init__(self, code):
        self.code = code


def _cache_key(app_id, secret):
    return app_id, hashlib.sha256(secret.encode()).hexdigest()


async def _token(app_id, secret, transport):
    key = _cache_key(app_id, secret)
    cached = _tokens.get(key)
    if cached and cached[1] > time.monotonic():
        return cached[0]
    try:
        status, answer = await call(
            "POST",
            TOKEN_URL,
            transport=transport,
            data={"grant_type": "client_credentials", "client_id": app_id, "client_secret": secret},
        )
    except Unknown:
        raise NotSent("token_unavailable") from None
    token = answer and answer.get("access_token")
    if status >= 400 or not answer or answer.get("code") != 0 or not isinstance(token, str):
        code = answer.get("code") if answer else None
        raise Rejected(f"feishu_token_{code}" if type(code) is int else "credential_invalid")
    lifetime = answer.get("expires_in")
    lifetime = lifetime if type(lifetime) is int and lifetime > TOKEN_MARGIN else 2 * TOKEN_MARGIN
    _tokens[key] = (token, time.monotonic() + lifetime - TOKEN_MARGIN)
    return token


async def _api(app_id, secret, method, path, *, transport=None, **request):
    """Call an Open API, refreshing the token once if the platform no longer accepts it."""
    for attempt in range(2):
        token = await _token(app_id, secret, transport)
        status, answer = await call(
            method,
            f"{API}{path}",
            transport=transport,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            **request,
        )
        code = answer.get("code") if answer else None
        if code in TOKEN_INVALID and not attempt:
            _tokens.pop(_cache_key(app_id, secret), None)
            continue
        return status, answer, code


def _message(content, uuid, **target):
    msg_type, body = feishu_content.render(content, uuid)
    serialized = json.dumps(body, ensure_ascii=False)
    payload = json.dumps(
        {**target, "msg_type": msg_type, "content": serialized, "uuid": uuid},
        ensure_ascii=False,
    ).encode()
    if len(payload) > MAX_BODY[msg_type]:
        raise Rejected("content_too_large")
    return payload


async def send(app_id, secret, receive_id_type, receive_id, content, uuid, *, transport=None):
    """Send once and return the provider message ID."""
    status, answer, code = await _api(
        app_id,
        secret,
        "POST",
        "/im/v1/messages",
        transport=transport,
        params={"receive_id_type": receive_id_type},
        content=_message(content, uuid, receive_id=receive_id),
    )
    return _sent(status, answer, code)


async def reply(app_id, secret, message_id, content, uuid, *, transport=None):
    """Reply once in the conversation of a received message; returns the new message ID."""
    status, answer, code = await _api(
        app_id,
        secret,
        "POST",
        f"/im/v1/messages/{message_id}/reply",
        transport=transport,
        content=_message(content, uuid),
    )
    return _sent(status, answer, code)


def _sent(status, answer, code):
    if code in TOKEN_INVALID:
        raise NotSent("credential_invalid")
    if code in RATE_LIMITED:
        raise NotSent("provider_rate_limited")
    if status >= 500 or answer is None:
        raise Unknown
    message_id = (answer.get("data") or {}).get("message_id")
    if code == 0 and status < 400 and isinstance(message_id, str):
        return message_id[:64]
    if code == 0:
        raise Unknown
    raise Rejected(f"feishu_{code}" if type(code) is int else "provider_rejected")


async def update_card(app_id, secret, message_id, card, *, transport=None):
    """Replace a shared card the app sent within the last 14 days, for every recipient."""
    content = json.dumps(card, ensure_ascii=False)
    if len(content.encode()) > MAX_BODY["interactive"]:
        raise Rejected("content_too_large")
    status, answer, code = await _api(
        app_id,
        secret,
        "PATCH",
        f"/im/v1/messages/{message_id}",
        transport=transport,
        content=json.dumps({"content": content}, ensure_ascii=False).encode(),
    )
    if code in TOKEN_INVALID:
        raise NotSent("credential_invalid")
    if code in RATE_LIMITED:
        raise NotSent("provider_rate_limited")
    if status >= 500 or answer is None:
        raise Unknown
    if code != 0:
        raise Rejected(f"feishu_{code}" if type(code) is int else "provider_rejected")


async def chats(app_id, secret, page_token=None, *, transport=None):
    """One page of the groups the app bot is in; p2p chats are never listed."""
    status, answer, code = await _api(
        app_id,
        secret,
        "GET",
        "/im/v1/chats",
        transport=transport,
        params={"page_size": 100, **({"page_token": page_token} if page_token else {})},
    )
    if status >= 500 or answer is None:
        raise Unknown
    if code != 0:
        raise Rejected(f"feishu_{code}" if type(code) is int else "provider_rejected")
    data = answer.get("data") or {}
    items = [
        {
            "chat_id": item["chat_id"],
            "name": str(item.get("name") or "")[:128],
            "external": bool(item.get("external")),
        }
        for item in data.get("items") or []
        if isinstance(item, dict)
        and isinstance(item.get("chat_id"), str)
        and item.get("chat_status", "normal") == "normal"
    ]
    more = data.get("has_more") and isinstance(data.get("page_token"), str)
    return {"items": items, "page_token": data["page_token"] if more else None}


async def open_id(app_id, secret, *, email=None, mobile=None, transport=None):
    """The user's open ID in this app, or None when Feishu returns no match.

    Users outside the app's contact data scope are not returned either.
    https://open.feishu.cn/document/server-docs/contact-v3/user/batch_get_id
    """
    status, answer, code = await _api(
        app_id,
        secret,
        "POST",
        "/contact/v3/users/batch_get_id",
        transport=transport,
        params={"user_id_type": "open_id"},
        content=json.dumps(
            {"emails": [email]} if email else {"mobiles": [mobile]}, ensure_ascii=False
        ).encode(),
    )
    if status >= 500 or answer is None:
        raise Unknown
    if code != 0:
        raise Rejected(f"feishu_{code}" if type(code) is int else "provider_rejected")
    for item in (answer.get("data") or {}).get("user_list") or []:
        found = isinstance(item, dict) and item.get("user_id")
        if isinstance(found, str) and found.startswith("ou_") and len(found) <= 64:
            return found
    return None


async def user_name(app_id, secret, open_id, *, transport=None):
    """The user's default name; needs a basic user info scope on top of contact access.

    https://open.feishu.cn/document/server-docs/contact-v3/user/get
    """
    status, answer, code = await _api(
        app_id,
        secret,
        "GET",
        f"/contact/v3/users/{open_id}",
        transport=transport,
        params={"user_id_type": "open_id"},
    )
    if status >= 500 or answer is None:
        raise Unknown
    if code != 0:
        raise Rejected(f"feishu_{code}" if type(code) is int else "provider_rejected")
    name = ((answer.get("data") or {}).get("user") or {}).get("name")
    return name[:128] if isinstance(name, str) and name else None

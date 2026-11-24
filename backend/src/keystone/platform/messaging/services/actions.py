"""Card actions, commands and content providers declared in code; composition merges them.

Handlers run as the executing identity their declaration names: `bound` is the sender's
bound Keystone user, `destination_user` the executing user configured on the group the
event came from. A handler only accepts the decision (checks business rules, records it,
queues follow-up work) inside its own transaction, must be idempotent by business state,
and raises ServiceError to refuse.

- card action: `async handler(runtime, identity, source_id, action_key, inbound_id)`
- command: `async handler(identity, inbound_id, command_key)`
- content provider: `async provider(identity, source_id)` returns content, or None once the
  content is no longer available; it runs as the delivery's creator and changes nothing.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from types import MappingProxyType

IDENTITIES = ("bound", "destination_user")
PLACES = ("p2p", "group")
COMMAND_KEY = re.compile(r"[a-z][a-z0-9_]{0,63}")
# Private messages starting with this word are binding codes.
RESERVED_PREFIX = "绑定"


@dataclass(frozen=True)
class MessageAction:
    # Matched against the delivery's source; the module is the declaring business namespace.
    source_module: str
    source_type: str
    key: str
    permission: str | None
    handler: Callable
    identity: str = "bound"


@dataclass(frozen=True)
class MessageCommand:
    module: str
    key: str
    # Matched against the whole message once mentions are removed and ends trimmed.
    text: str
    where: tuple[str, ...]
    permission: str | None
    handler: Callable
    accept_within: timedelta
    identity: str = "bound"
    # The platform replies to rejected, expired and failed commands when set.
    reply_failures: bool = False


@dataclass(frozen=True)
class MessageContent:
    source_module: str
    source_type: str
    provider: Callable


async def _acknowledge(runtime, identity, source_id, action_key, inbound_id):
    """The test card's button changes nothing; processing it proves the inbound path."""


TEST_CARD = {
    "kind": "card",
    "title": "Keystone 测试卡片",
    "body": "点击下方按钮，验证应用能收到卡片操作。",
    "actions": [{"key": "ack", "text": "确认收到", "style": "primary"}],
}
PLATFORM_ACTIONS = tuple(
    MessageAction("messaging", source_type, "ack", None, _acknowledge)
    for source_type in ("destination_test", "account_test")
)


def _check_permission(permission, permissions):
    if permission is not None and permission not in permissions:
        raise ValueError("Unknown message permission")


def build_actions(business_actions, permissions):
    result = {}
    for action in (*PLATFORM_ACTIONS, *business_actions):
        identifier = (action.source_module, action.source_type, action.key)
        if identifier in result:
            raise ValueError("Duplicate message action")
        if action.identity not in IDENTITIES:
            raise ValueError("Unknown message action identity")
        _check_permission(action.permission, permissions)
        result[identifier] = action
    return MappingProxyType(result)


def build_commands(commands, permissions):
    result, texts = {}, set()
    for command in commands:
        identifier = (command.module, command.key)
        if identifier in result:
            raise ValueError("Duplicate message command")
        text = command.text
        if not COMMAND_KEY.fullmatch(command.key) or not command.module:
            raise ValueError("Invalid message command key")
        if not text or text != text.strip() or len(text) > 64 or text in texts:
            raise ValueError("Message command text must be distinct, trimmed and short")
        if text.startswith(RESERVED_PREFIX):
            raise ValueError("Message command text is reserved for binding")
        where = set(command.where)
        if not where or not where <= set(PLACES) or len(where) != len(command.where):
            raise ValueError("Message command place must be p2p, group or both")
        if command.identity not in IDENTITIES:
            raise ValueError("Unknown message command identity")
        # Private chats have no destination, so nobody to act for the sender.
        if command.identity == "destination_user" and "p2p" in where:
            raise ValueError("destination_user commands are accepted in groups only")
        if command.accept_within <= timedelta(0):
            raise ValueError("Message command acceptance window must be positive")
        _check_permission(command.permission, permissions)
        result[identifier] = command
        texts.add(text)
    return MappingProxyType(result)


def build_contents(contents):
    result = {}
    for content in contents:
        identifier = (content.source_module, content.source_type)
        if identifier in result:
            raise ValueError("Duplicate message content provider")
        result[identifier] = content.provider
    return MappingProxyType(result)

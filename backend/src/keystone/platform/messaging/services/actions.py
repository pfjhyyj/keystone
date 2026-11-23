"""Card actions declared in code; composition merges them and rejects duplicates.

A handler runs as the clicking user's bound Keystone identity:
`async handler(runtime, identity, source_id, action_key, inbound_id)`. It only accepts the
decision (checks business rules, records it, queues follow-up work) inside its own
transaction, must be idempotent by business state, and raises ServiceError to refuse.
"""

from collections.abc import Callable
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class MessageAction:
    # Matched against the delivery's source; the module is the declaring business namespace.
    source_module: str
    source_type: str
    key: str
    permission: str | None
    handler: Callable


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


def build_actions(business_actions, permissions):
    result = {}
    for action in (*PLATFORM_ACTIONS, *business_actions):
        identifier = (action.source_module, action.source_type, action.key)
        if identifier in result:
            raise ValueError("Duplicate message action")
        if action.permission is not None and action.permission not in permissions:
            raise ValueError("Unknown message action permission")
        result[identifier] = action
    return MappingProxyType(result)

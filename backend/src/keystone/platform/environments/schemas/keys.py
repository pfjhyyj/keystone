import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType

KINDS = ("chromium",)


@dataclass(frozen=True)
class EnvironmentKey:
    """A code-declared environment purpose; users never create environments directly."""

    key: str
    name: str
    kinds: tuple[str, ...]
    # keep(session, environment_id, session_id) -> keep-until or None. Read-only and bounded:
    # it tells the idle stop that the business still waits on this session.
    keep: Callable[..., Awaitable[datetime | None]] | None = None


DEFAULT = EnvironmentKey("default", "默认环境", KINDS)


def environment_keys(businesses):
    """Combine the platform default with business keys named `<namespace>.<name>`."""
    keys = {DEFAULT.key: DEFAULT}
    for namespace, declared in businesses:
        pattern = re.compile(rf"{re.escape(namespace)}\.[a-z][a-z0-9_]*")
        for item in declared:
            if (
                not pattern.fullmatch(item.key)
                or len(item.key) > 64
                or item.key in keys
                or not item.name.strip()
                or not item.kinds
                or not set(item.kinds) <= set(KINDS)
            ):
                raise ValueError("Invalid or duplicate environment key")
            keys[item.key] = item
    return MappingProxyType(keys)

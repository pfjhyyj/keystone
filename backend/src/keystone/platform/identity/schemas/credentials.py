"""In-memory credential input; never a durable execution parameter."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Credential:
    kind: str
    secret: str

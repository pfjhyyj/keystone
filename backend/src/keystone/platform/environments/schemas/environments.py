from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

type SessionState = Literal["starting", "ready", "stopping", "review", "stopped", "failed"]
type SessionPurpose = Literal["interactive", "backup"]
type EnvironmentKind = Literal["chromium"]
OCCUPIED = ("starting", "ready", "stopping", "review")


class SessionView(BaseModel):
    id: UUID
    purpose: SessionPurpose
    state: SessionState
    can_stop: bool
    restored_from: datetime | None = None


class EnvironmentView(BaseModel):
    """`id` is empty for a declared environment that has not been used yet."""

    id: UUID | None
    kind: EnvironmentKind
    key: str
    name: str
    session: SessionView | None = None
    backup_at: datetime | None = None
    backup_failed: bool = False
    backup_enabled: bool = False


class EnvironmentRequest(BaseModel):
    kind: EnvironmentKind
    key: str

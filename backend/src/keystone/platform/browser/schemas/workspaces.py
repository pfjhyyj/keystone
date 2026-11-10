from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

type RunState = Literal["starting", "ready", "stopping", "review", "stopped", "failed"]
type RunPurpose = Literal["browse", "backup"]
OCCUPIED = ("starting", "ready", "stopping", "review")


class RunView(BaseModel):
    id: UUID
    purpose: RunPurpose
    state: RunState
    can_stop: bool
    restored_from: datetime | None = None


class WorkspaceView(BaseModel):
    id: UUID
    name: str
    run: RunView | None
    backup_at: datetime | None = None
    backup_failed: bool = False
    backup_enabled: bool = False

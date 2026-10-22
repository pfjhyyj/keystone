"""Lightweight declarations: importing them never registers DBOS workflows."""

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, RootModel


class NoInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


@dataclass(frozen=True)
class TaskDefinition:
    key: str
    entrypoint: str
    input_model: type[BaseModel]
    queue: str
    timeout_seconds: int | None = None

    def __post_init__(self):
        for value in (self.key, self.entrypoint, self.queue):
            _name(value)
        if (
            not isinstance(self.input_model, type)
            or self.input_model is BaseModel
            or not issubclass(self.input_model, BaseModel)
            or issubclass(self.input_model, RootModel)
            or self.input_model.model_config.get("extra") != "forbid"
        ):
            raise ValueError("Task input must be an object model with extra fields forbidden")
        if self.timeout_seconds is not None:
            _positive_integer(self.timeout_seconds)


@dataclass(frozen=True)
class TaskBinding:
    definition: TaskDefinition
    workflow: Callable[..., Awaitable[None]]
    enabled: bool = True
    monitor_key: str | None = None

    def __post_init__(self):
        if not callable(self.workflow) or type(self.enabled) is not bool:
            raise ValueError("Invalid task binding")
        if self.monitor_key is not None:
            _name(self.monitor_key)


@dataclass(frozen=True)
class QueueDefinition:
    name: str
    worker_concurrency: int
    global_concurrency: int

    def __post_init__(self):
        _name(self.name)
        _positive_integer(self.worker_concurrency)
        _positive_integer(self.global_concurrency)


@dataclass(frozen=True)
class ScheduleDefinition:
    name: str
    tick_entrypoint: str
    task_key: str
    cron: str = "*/5 * * * * *"
    payload: dict = field(default_factory=dict)
    timezone: str = "UTC"
    max_lateness_seconds: int | None = 10
    run_on_startup: bool = True
    backfill: bool = False
    overlap: Literal["allow", "coalesce"] = "coalesce"

    def __post_init__(self):
        for value in (self.name, self.tick_entrypoint, self.task_key, self.cron, self.timezone):
            _name(value)
        if self.overlap not in {"allow", "coalesce"}:
            raise ValueError("Invalid schedule overlap policy")
        if self.max_lateness_seconds is not None and (
            type(self.max_lateness_seconds) is not int or self.max_lateness_seconds < 0
        ):
            raise ValueError("Invalid schedule lateness limit")
        if type(self.run_on_startup) is not bool or type(self.backfill) is not bool:
            raise ValueError("Invalid schedule flags")
        if not isinstance(self.payload, dict):
            raise ValueError("Schedule payload must be an object")
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Invalid schedule timezone") from None


@dataclass(frozen=True)
class ExecutionRef:
    workflow_id: str


def _name(value):
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError("Execution names must be nonempty strings without surrounding whitespace")


def _positive_integer(value):
    if type(value) is not int or value < 1:
        raise ValueError("Execution limits must be positive integers")


def task_payload(task: TaskDefinition, payload: BaseModel | dict) -> dict:
    value = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload
    if not isinstance(value, dict):
        raise ValueError("Task payload must be an object")
    value = task.input_model.model_validate(value).model_dump(mode="json")
    if not isinstance(value, dict):
        raise ValueError("Serialized task payload must be an object")
    raw = json.dumps(value, allow_nan=False, ensure_ascii=False, separators=(",", ":")).encode()
    if len(raw) > 65536:
        raise ValueError("Task payload exceeds 64 KiB")
    return value

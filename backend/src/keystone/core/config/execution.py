import re

from pydantic import BaseModel, Field, field_validator, model_validator


class ExecutionSettings(BaseModel):
    worker_id: str = "keystone-worker"
    worker_shutdown_seconds: int = Field(default=10, ge=1, le=600)
    flow_worker_concurrency: int = Field(default=2, ge=1, le=16)
    flow_global_concurrency: int = Field(default=4, ge=1, le=64)

    @field_validator("worker_id")
    @classmethod
    def valid_worker_id(cls, value):
        if not re.fullmatch(r"[a-z][a-z0-9_-]{2,63}", value):
            raise ValueError("Configure a valid fixed worker identity")
        return value

    @model_validator(mode="after")
    def valid_flow_concurrency(self):
        if self.flow_worker_concurrency > self.flow_global_concurrency:
            raise ValueError("Flow worker concurrency must not exceed global concurrency")
        return self

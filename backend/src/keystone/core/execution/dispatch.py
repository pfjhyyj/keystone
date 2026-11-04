"""Validated native enqueue, never an alternative workflow runner."""

from dbos import DBOS, DBOSClient
from pydantic import BaseModel
from sqlalchemy import create_engine

from keystone.core.asyncio import blocking
from keystone.core.execution.contracts import (
    ExecutionRef,
    TaskBinding,
    TaskDefinition,
    task_payload,
)


def _request(task, payload, *, execution_key, application_version, coalesce_key):
    if not execution_key or not application_version:
        raise ValueError("Execution key and application version are required")
    value = task_payload(task, payload)
    options = {
        "workflow_name": task.entrypoint,
        "queue_name": task.queue,
        "workflow_id": execution_key,
        "app_version": application_version,
        "workflow_id_reuse_policy": "return-existing",
    }
    if coalesce_key is not None:
        if not coalesce_key:
            raise ValueError("Coalesce key must be nonempty")
        options["deduplication_id"] = f"{coalesce_key}:{application_version}"
        options["duplication_policy"] = "return-existing"
    if task.timeout_seconds is not None:
        options["workflow_timeout"] = task.timeout_seconds
    return options, value


class TaskClient:
    """Persist tasks from API/CLI without importing or starting Worker workflows."""

    def __init__(self, settings, *, application_version):
        self.application_version = application_version
        self.engine = create_engine(
            settings.database_url.get_secret_value(),
            pool_size=5,
            max_overflow=0,
            pool_timeout=3,
            pool_pre_ping=True,
            connect_args={
                "connect_timeout": 3,
                "options": "-cstatement_timeout=3000 -clock_timeout=3000",
            },
            hide_parameters=True,
        )
        self.client = DBOSClient(
            system_database_engine=self.engine,
            application_name="keystone",
            lazy=True,
            retry_connection_errors=False,
        )

    async def enqueue(
        self,
        task: TaskDefinition,
        payload: BaseModel | dict,
        *,
        execution_key: str,
        coalesce_key: str | None = None,
    ) -> ExecutionRef:
        # The definition comes from trusted application code, never a request's import path.
        options, value = _request(
            task,
            payload,
            execution_key=execution_key,
            application_version=self.application_version(),
            coalesce_key=coalesce_key,
        )
        handle = await blocking(self.client.enqueue, options, value)
        return ExecutionRef(handle.get_workflow_id())

    async def close(self):
        try:
            await blocking(self.client.destroy)
        finally:
            # DBOS never disposes a caller-owned engine.
            await blocking(self.engine.dispose)


async def enqueue_task(
    task: TaskBinding,
    payload: BaseModel | dict,
    *,
    execution_key: str,
    coalesce_key: str | None = None,
) -> ExecutionRef:
    if not task.enabled:
        raise ValueError("Task is disabled")
    options, value = _request(
        task.definition,
        payload,
        execution_key=execution_key,
        application_version=DBOS.application_version,
        coalesce_key=coalesce_key,
    )
    handle = await DBOS.enqueue_workflow_with_options_async(options, value)
    return ExecutionRef(handle.get_workflow_id())

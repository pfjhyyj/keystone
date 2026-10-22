"""Validated native enqueue, never an alternative workflow runner."""

from dbos import DBOS
from pydantic import BaseModel

from keystone.core.execution.contracts import ExecutionRef, TaskBinding, task_payload


async def enqueue_task(
    task: TaskBinding,
    payload: BaseModel | dict,
    *,
    execution_key: str,
    coalesce_key: str | None = None,
) -> ExecutionRef:
    if not task.enabled:
        raise ValueError("Task is disabled")
    if not execution_key or not DBOS.application_version:
        raise ValueError("Execution key and application version are required")
    value = task_payload(task.definition, payload)
    options = {
        "workflow_name": task.definition.entrypoint,
        "queue_name": task.definition.queue,
        "workflow_id": execution_key,
        "app_version": DBOS.application_version,
        "workflow_id_reuse_policy": "return-existing",
    }
    if coalesce_key is not None:
        if not coalesce_key:
            raise ValueError("Coalesce key must be nonempty")
        options["deduplication_id"] = f"{coalesce_key}:{DBOS.application_version}"
        options["duplication_policy"] = "return-existing"
    if task.definition.timeout_seconds is not None:
        options["workflow_timeout"] = task.definition.timeout_seconds
    handle = await DBOS.enqueue_workflow_with_options_async(options, value)
    return ExecutionRef(handle.get_workflow_id())

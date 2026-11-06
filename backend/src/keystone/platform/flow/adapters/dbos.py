"""Public DBOS client boundary; never starts an executor in API."""

import asyncio

from dbos import DBOSClient
from dbos import error as dbos_error

from keystone.core.errors import ServiceError
from keystone.platform.flow.schemas.errors import ERRORS as ERRORS

QUEUE = "keystone-flows"


def workflow_id(identifier):
    return f"flow:{identifier}"


class Engine:
    def __init__(self, settings):
        self.client = DBOSClient(
            system_database_url=settings.database_url.get_secret_value(),
            application_name="keystone",
            lazy=True,
            retry_connection_errors=False,
            observability_query_timeout_sec=5,
        )

    async def enqueue(self, run):
        options = {
            "workflow_name": run.definition_snapshot["entrypoint"],
            "queue_name": run.definition_snapshot["queue"],
            "workflow_id": workflow_id(run.id),
            "app_version": run.execution_version,
            "workflow_timeout": run.definition_snapshot["timeout_seconds"],
        }
        await self.client.enqueue_async(options, str(run.id))

    async def state(self, identifier):
        rows = await self.client.list_workflows_async(
            workflow_ids=[workflow_id(identifier)], limit=1, load_input=False
        )
        if not rows:
            return None
        row = rows[0]
        error = row.error
        if isinstance(error, dbos_error.DBOSMaxStepRetriesExceeded) and error.errors:
            error = error.errors[-1]
        code = error.code if isinstance(error, ServiceError) else str(error)
        return {
            "status": row.status,
            "result": row.output,
            "error_code": code if code in ERRORS else "flow_execution_failed",
            "version": row.app_version,
            "name": row.name,
        }

    async def cancel(self, identifier):
        await self.client.cancel_workflow_async(workflow_id(identifier), cancel_children=True)

    async def steps(self, identifier):
        # Follow only engine-recorded descendants of the authorized root; bounded traversal.
        pending, visited, result = [workflow_id(identifier)], set(), []
        while pending and len(visited) < 32:
            current = pending.pop(0)
            if current in visited:
                continue
            visited.add(current)
            rows = await self.client.list_workflow_steps_async(current, load_output=False)
            for row in rows:
                if len(result) == 500:
                    return {"items": result, "truncated": True}
                result.append(
                    {
                        "workflow_id": current,
                        "step_id": row.get("function_id"),
                        "name": row.get("function_name"),
                        "started_at_epoch_ms": row.get("started_at_epoch_ms"),
                        "completed_at_epoch_ms": row.get("completed_at_epoch_ms"),
                    }
                )
                if row.get("child_workflow_id"):
                    pending.append(row["child_workflow_id"])
        return {"items": result, "truncated": bool(pending)}

    async def close(self):
        await asyncio.to_thread(self.client.destroy)

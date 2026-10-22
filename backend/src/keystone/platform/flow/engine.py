"""Adapted from Ether's public DBOS client boundary; never starts an executor in API."""

import asyncio

from dbos import DBOSClient

from keystone.core.errors import ServiceError

QUEUE = "keystone-flows"
ERRORS = {
    "execution_not_authorized",
    "flow_cancelled",
    "execution_version_unavailable",
    "flow_invalid_output",
    "flow_budget_exceeded",
    "flow_timeout",
    "process_timeout",
    "process_output_limit",
    "process_file_limit",
    "process_unavailable",
    "model_result_unavailable",
    "model_not_found",
    "model_disabled",
    "model_configuration_changed",
    "model_target_changed",
    "model_input_too_large",
    "model_response_too_large",
    "model_stream_incomplete",
    "model_interrupted",
    "model_timeout",
    "model_response_unknown",
    "model_provider_unavailable",
    "model_target_not_allowed",
    "model_credential_unavailable",
    "model_insufficient_credits",
    "model_authentication_failed",
    "model_access_denied",
    "model_invalid_request",
    "upstream_model_not_found",
    "model_rate_limited",
    "model_upstream_error",
    "flow_input_too_large",
    "flow_invalid_text",
    "flow_execution_failed",
}


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

    async def enqueue(self, run, definition):
        await self.client.enqueue_async(
            {
                "workflow_name": definition.entrypoint,
                "queue_name": QUEUE,
                "workflow_id": workflow_id(run.id),
                "app_version": run.execution_version,
                "workflow_timeout": definition.timeout_seconds,
            },
            str(run.id),
        )

    async def state(self, identifier):
        rows = await self.client.list_workflows_async(
            workflow_ids=[workflow_id(identifier)], limit=1, load_input=False
        )
        if not rows:
            return None
        row = rows[0]
        code = row.error.code if isinstance(row.error, ServiceError) else str(row.error)
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

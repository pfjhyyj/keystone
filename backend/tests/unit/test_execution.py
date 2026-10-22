"""Execution boundaries: reject invalid inputs and sanitize before persistence."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel, Field, RootModel, model_serializer

from keystone.core.execution import dispatch, steps
from keystone.core.execution.contracts import (
    NoInput,
    QueueDefinition,
    ScheduleDefinition,
    TaskBinding,
    TaskDefinition,
    task_payload,
)
from keystone.core.execution.planning import queue_budgets
from keystone.platform.model_access.tasks.definitions import COST_SCOPE


async def nothing(payload):
    pass


async def test_enqueue_validates_before_io_and_returns_actual_coalesced_execution(monkeypatch):
    enqueue = AsyncMock(return_value=SimpleNamespace(get_workflow_id=lambda: "existing"))
    monkeypatch.setattr(
        dispatch,
        "DBOS",
        SimpleNamespace(
            application_version="release",
            enqueue_workflow_with_options_async=enqueue,
        ),
    )
    binding = TaskBinding(COST_SCOPE, nothing)
    for payload, task in [
        ({"scope": "scope", "credentials": "secret"}, binding),
        ({"scope": ""}, binding),
        ({"scope": "scope"}, replace(binding, enabled=False)),
    ]:
        with pytest.raises(ValueError):
            await dispatch.enqueue_task(task, payload, execution_key="request")
    enqueue.assert_not_called()
    reference = await dispatch.enqueue_task(
        binding,
        {"scope": "scope"},
        execution_key="request",
        coalesce_key="scope",
    )
    assert reference.workflow_id == "existing"
    options, payload = enqueue.call_args.args
    assert options["workflow_id"] == "request"
    assert options["deduplication_id"] == "scope:release"
    assert options["duplication_policy"] == "return-existing"
    assert payload == {"scope": "scope"}
    await dispatch.enqueue_task(binding, {"scope": "scope"}, execution_key="standalone")
    options = enqueue.call_args.args[0]
    assert options["workflow_id_reuse_policy"] == "return-existing"
    assert "duplication_policy" not in options and "deduplication_id" not in options


@pytest.mark.parametrize(
    "changes",
    [
        {"key": " "},
        {"entrypoint": ""},
        {"queue": ""},
        {"timeout_seconds": 0},
        {"timeout_seconds": True},
        {"timeout_seconds": float("inf")},
        {"input_model": BaseModel},
        {"input_model": RootModel[list[str]]},
    ],
)
def test_invalid_task_declarations_fail_early(changes):
    with pytest.raises(ValueError):
        replace(COST_SCOPE, **changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"overlap": "coalese"},
        {"max_lateness_seconds": -1},
        {"max_lateness_seconds": True},
        {"timezone": "Missing/Zone"},
        {"payload": []},
        {"backfill": 1},
    ],
)
def test_invalid_schedule_policies_fail_early(changes):
    with pytest.raises(ValueError):
        ScheduleDefinition("plan", "tick", "task", **changes)


def test_task_payload_rejects_serializers_that_break_object_protocol():
    class WrongSerialization(NoInput):
        @model_serializer
        def serialize(self):
            return ["unexpected"]

    task = TaskDefinition("task", "workflow", WrongSerialization, "queue")
    with pytest.raises(ValueError, match="Serialized task payload"):
        task_payload(task, {})


def test_recursive_object_inputs_are_valid_task_payloads():
    class NestedInput(NoInput):
        children: list["NestedInput"] = Field(default_factory=list)

    # A recursive object schema has a top-level $ref rather than type=object.
    task = TaskDefinition("nested", "nested.workflow", NestedInput, "queue")
    assert task_payload(task, {"children": [{}]}) == {"children": [{"children": []}]}


def test_plan_checks_bindings_queues_schedules_and_disabled_queue_consumption():
    async def workflow(payload):
        pass

    workflow.dbos_function_name = COST_SCOPE.entrypoint
    binding = TaskBinding(COST_SCOPE, workflow)
    queue = QueueDefinition(COST_SCOPE.queue, 1, 2)
    plan = ScheduleDefinition("plan", "tick", COST_SCOPE.key, payload={"scope": "scope"})
    assert queue_budgets([binding], [queue], [plan]) == {queue.name: (1, 2)}
    assert queue_budgets([replace(binding, enabled=False)], [queue], [plan]) == {}
    cases = [
        ([binding, binding], [queue], [plan]),
        ([binding], [queue, queue], [plan]),
        ([binding], [], [plan]),
        ([binding], [queue], [plan, plan]),
        ([binding], [queue], [replace(plan, task_key="missing")]),
        ([binding], [queue], [replace(plan, tick_entrypoint=COST_SCOPE.entrypoint)]),
        ([binding], [queue], [replace(plan, payload={"secret": "unexpected"})]),
        ([replace(binding, workflow=nothing)], [queue], [plan]),
    ]
    for bindings, queues, schedules in cases:
        with pytest.raises(ValueError):
            queue_budgets(bindings, queues, schedules)


async def test_safe_step_cleans_error_before_dbos_receives_it(monkeypatch, caplog):
    persisted = []

    def boundary(**options):
        assert options["retries_allowed"] is False

        def decorate(function):
            async def checkpoint():
                try:
                    return await function()
                except Exception as error:
                    persisted.append(error)
                    raise

            return checkpoint

        return decorate

    monkeypatch.setattr(steps, "DBOS", SimpleNamespace(step=boundary))

    @steps.safe_step(name="boundary", error_code="safe_failure")
    async def fail():
        raise RuntimeError("postgresql://user:private-secret@host/database")

    with pytest.raises(RuntimeError, match="^safe_failure$"):
        await fail()
    assert str(persisted[0]) == "safe_failure"
    assert persisted[0].__suppress_context__
    assert "private-secret" not in caplog.text

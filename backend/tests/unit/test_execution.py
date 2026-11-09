"""Execution boundaries: reject invalid inputs and sanitize before persistence."""

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import BaseModel, Field, RootModel, model_serializer

from keystone.core.config import Settings
from keystone.core.execution import dispatch, runtime, schedules, steps
from keystone.core.execution.contracts import (
    NoInput,
    QueueDefinition,
    ScheduleDefinition,
    TaskBinding,
    TaskDefinition,
    task_payload,
)
from keystone.core.execution.history import HISTORY_SCHEDULE
from keystone.core.execution.planning import queue_budgets
from keystone.core.time import utcnow
from keystone.platform.files.tasks.definitions import CLEANUP_SCHEDULE
from keystone.platform.flow.tasks.definitions import RECONCILE_SCHEDULE
from keystone.platform.model_access.tasks.definitions import COST_SCHEDULE, COST_SCOPE


@pytest.mark.parametrize(
    "plan,cron,lateness",
    [
        (RECONCILE_SCHEDULE, "*/5 * * * * *", 10),
        (CLEANUP_SCHEDULE, "0 */5 * * * *", 60),
        (COST_SCHEDULE, "0 * * * * *", 45),
        (HISTORY_SCHEDULE, "0 0 * * * *", 300),
    ],
)
def test_maintenance_schedule_policies(plan, cron, lateness):
    assert plan.cron == cron
    assert plan.max_lateness_seconds == lateness
    assert plan.timezone == "UTC"
    assert plan.run_on_startup is True
    assert plan.backfill is False
    assert plan.overlap == "coalesce"


@pytest.mark.parametrize(
    "plan", [RECONCILE_SCHEDULE, CLEANUP_SCHEDULE, COST_SCHEDULE, HISTORY_SCHEDULE]
)
async def test_maintenance_tick_accepts_lateness_boundary_and_skips_older(monkeypatch, plan):
    now = utcnow()
    workflows = {}

    def workflow(*, name):
        def decorate(function):
            workflows[name] = function
            return function

        return decorate

    monkeypatch.setattr(schedules, "DBOS", SimpleNamespace(workflow=workflow, workflow_id="tick"))
    monkeypatch.setattr(schedules, "safe_step", lambda **kwargs: lambda function: function)
    monkeypatch.setattr(schedules, "utcnow", lambda: now)
    enqueue = AsyncMock()
    monkeypatch.setattr(schedules, "enqueue_task", enqueue)
    definition = TaskDefinition(plan.task_key, "maintenance", NoInput, "maintenance")
    schedules.register_schedule(plan, TaskBinding(definition, nothing), execution_prefix="test")
    tick = workflows[plan.tick_entrypoint]
    await tick(now - timedelta(seconds=plan.max_lateness_seconds), None)
    enqueue.assert_awaited_once()
    assert enqueue.call_args.kwargs["coalesce_key"] == plan.name
    enqueue.reset_mock()
    await tick(now - timedelta(seconds=plan.max_lateness_seconds, microseconds=1), None)
    enqueue.assert_not_awaited()


async def test_worker_passes_supported_executor_limit_before_launch(monkeypatch):
    client = SimpleNamespace(register_queue_async=AsyncMock(), destroy=Mock())
    monkeypatch.setattr(runtime, "DBOSClient", Mock(return_value=client))
    lease = AsyncMock()

    def construct(*, config):
        assert config["max_executor_threads"] == 32
        assert config["scheduler_polling_interval_sec"] == 1
        lease.assert_awaited_once()
        assert client.register_queue_async.await_args_list == [
            (("maintenance",), {"worker_concurrency": 1, "global_concurrency": 1})
        ]
        # Stop before native launch: this unit test must never connect to a database.
        raise RuntimeError("configuration checked")

    monkeypatch.setattr(runtime, "DBOS", construct)
    settings = Settings(config_file=None, database_url="postgresql+psycopg://localhost/unused")
    with pytest.raises(RuntimeError, match="configuration checked"):
        async with runtime.lifespan(
            settings, application_version="release", queues={"maintenance": (1, 1)}, lease=lease
        ):
            pytest.fail("Native executor must not launch")
    client.destroy.assert_called_once()


async def nothing(payload):
    pass


async def test_client_uses_lightweight_definition_and_validates_before_native_enqueue(monkeypatch):
    native = SimpleNamespace(
        enqueue=Mock(return_value=SimpleNamespace(get_workflow_id=lambda: "existing")),
        destroy=Mock(),
    )
    constructor = Mock(return_value=native)
    engine = SimpleNamespace(dispose=Mock())
    create_engine = Mock(return_value=engine)
    monkeypatch.setattr(dispatch, "DBOSClient", constructor)
    monkeypatch.setattr(dispatch, "create_engine", create_engine)
    client = dispatch.TaskClient(
        Settings(config_file=None, database_url="postgresql+psycopg://localhost/unused"),
        application_version=lambda: "release",
    )
    try:
        for payload in ({"scope": "", "secret": "private"}, {"scope": ""}):
            with pytest.raises(ValueError):
                await client.enqueue(COST_SCOPE, payload, execution_key="request")
        native.enqueue.assert_not_called()
        ref = await client.enqueue(
            COST_SCOPE, {"scope": "scope"}, execution_key="request", coalesce_key="scope"
        )
        assert ref.workflow_id == "existing"
        options, payload = native.enqueue.call_args.args
        assert options == {
            "workflow_name": COST_SCOPE.entrypoint,
            "queue_name": COST_SCOPE.queue,
            "workflow_id": "request",
            "app_version": "release",
            "workflow_id_reuse_policy": "return-existing",
            "deduplication_id": "scope:release",
            "duplication_policy": "return-existing",
        }
        assert payload == {"scope": "scope"}
        assert constructor.call_args.kwargs["lazy"] is True
        assert constructor.call_args.kwargs["retry_connection_errors"] is False
        assert constructor.call_args.kwargs["system_database_engine"] is engine
        assert create_engine.call_args.kwargs["connect_args"]["connect_timeout"] == 3
    finally:
        await client.close()
    native.destroy.assert_called_once()
    engine.dispose.assert_called_once()


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
    for bindings, queues, plans in cases:
        with pytest.raises(ValueError):
            queue_budgets(bindings, queues, plans)


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

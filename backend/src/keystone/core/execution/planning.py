"""Validate explicit declarations before the executor starts consuming work."""

from keystone.core.execution.contracts import task_payload


def _unique(values, label):
    if len(values) != len(set(values)):
        raise ValueError(f"Duplicate {label}")


def queue_budgets(bindings, queues, schedules, *, other_entrypoints=()):
    tasks = {binding.definition.key: binding.definition for binding in bindings}
    _unique([binding.definition.key for binding in bindings], "task key")
    _unique([queue.name for queue in queues], "queue name")
    _unique([plan.name for plan in schedules], "schedule name")
    _unique(
        [task.entrypoint for task in tasks.values()]
        + [plan.tick_entrypoint for plan in schedules]
        + list(other_entrypoints),
        "execution entrypoint",
    )
    budgets = {q.name: (q.worker_concurrency, q.global_concurrency) for q in queues}
    for binding in bindings:
        task = binding.definition
        if getattr(binding.workflow, "dbos_function_name", None) != task.entrypoint:
            raise ValueError("Task binding must match its registered DBOS workflow")
        if task.queue not in budgets:
            raise ValueError("Task queue budget missing")
    for plan in schedules:
        if plan.task_key not in tasks:
            raise ValueError("Schedule target task missing")
        task_payload(tasks[plan.task_key], plan.payload)
    enabled_queues = {binding.definition.queue for binding in bindings if binding.enabled}
    return {name: budget for name, budget in budgets.items() if name in enabled_queues}

from keystone.core.execution.contracts import NoInput, ScheduleDefinition, TaskDefinition

RECONCILE = TaskDefinition(
    key="flow.reconcile",
    entrypoint="keystone.flow.reconcile_pass",
    input_model=NoInput,
    queue="keystone-flow-maintenance",
)
RECONCILE_SCHEDULE = ScheduleDefinition(
    name="keystone.flow.reconcile",
    tick_entrypoint="keystone.flow.reconcile_tick",
    task_key=RECONCILE.key,
)

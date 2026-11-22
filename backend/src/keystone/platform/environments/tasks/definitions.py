from keystone.core.execution.contracts import NoInput, ScheduleDefinition, TaskDefinition

IDLE_STOP = TaskDefinition(
    key="environments.idle_stop",
    entrypoint="keystone.environments.idle_stop_pass",
    input_model=NoInput,
    queue="keystone-environments-maintenance",
)
IDLE_STOP_SCHEDULE = ScheduleDefinition(
    name="keystone.environments.idle_stop",
    tick_entrypoint="keystone.environments.idle_stop_tick",
    task_key=IDLE_STOP.key,
    cron="0 * * * * *",
    max_lateness_seconds=45,
)

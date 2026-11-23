from keystone.core.execution.contracts import NoInput, ScheduleDefinition, TaskDefinition

DISPATCH = TaskDefinition(
    key="messaging.dispatch",
    entrypoint="keystone.messaging.dispatch_pass",
    input_model=NoInput,
    queue="keystone-messaging",
)
DISPATCH_SCHEDULE = ScheduleDefinition(
    name="keystone.messaging.dispatch",
    tick_entrypoint="keystone.messaging.dispatch_tick",
    task_key=DISPATCH.key,
    cron="*/5 * * * * *",
    max_lateness_seconds=10,
)

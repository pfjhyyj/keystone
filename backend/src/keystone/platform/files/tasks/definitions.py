from keystone.core.execution.contracts import NoInput, ScheduleDefinition, TaskDefinition

CLEANUP = TaskDefinition(
    key="files.cleanup",
    entrypoint="keystone.files.cleanup_pass",
    input_model=NoInput,
    queue="keystone-files-maintenance",
)
CLEANUP_SCHEDULE = ScheduleDefinition(
    name="keystone.files.cleanup",
    tick_entrypoint="keystone.files.cleanup_tick",
    task_key=CLEANUP.key,
)

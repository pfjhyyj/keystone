from pydantic import BaseModel, ConfigDict, Field

from keystone.core.execution.contracts import NoInput, ScheduleDefinition, TaskDefinition


class CostScopeInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    scope: str = Field(min_length=1, max_length=64)


COST_SCAN = TaskDefinition(
    key="model_access.cost_scan",
    entrypoint="keystone.model_access.cost_scan",
    input_model=NoInput,
    queue="keystone-model-maintenance",
)
COST_SCOPE = TaskDefinition(
    key="model_access.cost_scope",
    entrypoint="keystone.model_access.cost_scope",
    input_model=CostScopeInput,
    queue="keystone-system",
)
COST_SCHEDULE = ScheduleDefinition(
    name="keystone.model_access.reconcile_costs",
    tick_entrypoint="keystone.model_access.cost_tick",
    task_key=COST_SCAN.key,
    cron="0 * * * * *",
    max_lateness_seconds=45,
)

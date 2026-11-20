"""Subprocess Worker cadence for persisted maintenance schedules.

Production cron values stay in their definitions. Tests still exercise real DBOS
schedules, but a tick must arrive within their wait budget. Install before any
Worker module imports the definitions.
"""

from dataclasses import replace

TEST_CRON = "*/5 * * * * *"


def install():
    from keystone.platform.files.tasks import definitions as files
    from keystone.platform.model_access.tasks import definitions as costs

    files.CLEANUP_SCHEDULE = replace(
        files.CLEANUP_SCHEDULE, cron=TEST_CRON, max_lateness_seconds=10
    )
    costs.COST_SCHEDULE = replace(costs.COST_SCHEDULE, cron=TEST_CRON, max_lateness_seconds=10)

"""Flow binding, recovery and business queue setup inside the Worker runtime."""

import logging
from contextlib import asynccontextmanager

from dbos import DBOS

from keystone.platform.flow import host
from keystone.platform.flow.engine import QUEUE as QUEUE
from keystone.platform.model_access.services.recovery import recover_interrupted_calls

log = logging.getLogger(__name__)


@asynccontextmanager
async def prepare(runtime, *, lease):
    # The Worker holds its fixed-identity locks throughout this context.
    # Bind services and close interrupted calls before DBOS starts recovery.
    host.bind(runtime)
    try:
        await recover_interrupted_calls(runtime.database, runtime.worker_identity, lease=lease)
        yield
    finally:
        host.bind(None)


def report_ready():
    log.info("flow_worker_ready executor=%s version=%s", DBOS.executor_id, DBOS.application_version)

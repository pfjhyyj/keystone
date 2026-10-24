"""Flow binding, recovery and business queue setup inside the Worker runtime."""

import logging
from contextlib import asynccontextmanager

from dbos import DBOS

from keystone.platform.flow.adapters.dbos import QUEUE as QUEUE
from keystone.platform.flow.workflows import context as host
from keystone.platform.model_access.services.recovery import recover_interrupted_calls

log = logging.getLogger(__name__)


@asynccontextmanager
async def prepare(runtime, *, lease):
    # The Worker holds its fixed-identity locks throughout this context.
    # Bind services and close interrupted calls before DBOS starts recovery.
    host._bind(runtime)
    try:
        await recover_interrupted_calls(runtime.database, runtime.worker_identity, lease=lease)
        yield
    finally:
        host._bind(None)


def report_ready():
    log.info("flow_worker_ready executor=%s version=%s", DBOS.executor_id, DBOS.application_version)

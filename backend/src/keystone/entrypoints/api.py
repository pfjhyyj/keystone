import logging

import uvicorn

from keystone.bootstrap import create_app
from keystone.core.config import Settings
from keystone.core.errors import ServiceError
from keystone.core.logging import configure_logging, safe_exception
from keystone.entrypoints.configuration import load_settings


def main() -> None:
    try:
        settings = load_settings(Settings)
    except ServiceError as error:
        logging.getLogger(__name__).error(
            "configuration_failed code=%s detail=%s",
            error.code,
            error.detail,
            extra={"exception": safe_exception(error)},
        )
        raise SystemExit(1) from None
    configure_logging(settings.log_level)
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_config=None,
        access_log=False,
        proxy_headers=False,
        loop="keystone.core.asyncio:new_event_loop",
    )

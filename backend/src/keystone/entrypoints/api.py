import logging

import uvicorn

from keystone.bootstrap import create_app
from keystone.core.config import Settings
from keystone.core.logging import configure_logging, safe_exception


def main() -> None:
    try:
        settings = Settings()
    except Exception as error:
        logging.getLogger(__name__).error(
            "configuration_failed type=%s",
            type(error).__name__,
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

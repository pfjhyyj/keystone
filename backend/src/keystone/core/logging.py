import json
import logging
from datetime import UTC, datetime


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # Callers log safe event names, not request bodies, DSNs or exception text.
        return json.dumps(
            {
                "time": datetime.now(UTC).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            },
            ensure_ascii=False,
        )


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)
    # Readiness constructs a migration context every time; keep its INFO noise out of logs.
    logging.getLogger("alembic.runtime.migration").setLevel(logging.WARNING)
    logging.getLogger("casbin").setLevel(logging.WARNING)
    # SDK DEBUG logs can contain messages and headers; never inherit application DEBUG.
    for name in ("openai", "httpx2", "httpcore2"):
        logging.getLogger(name).setLevel(logging.WARNING)

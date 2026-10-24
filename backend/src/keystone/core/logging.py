import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from traceback import walk_tb

SOURCE_ROOT = Path(__file__).resolve().parents[1]


def exception_chain(error: BaseException):
    """Bound explicit and implicit causes, respecting deliberate context suppression."""
    seen = set()
    while error is not None and id(error) not in seen and len(seen) < 8:
        seen.add(id(error))
        yield error
        error = error.__cause__ or (None if error.__suppress_context__ else error.__context__)


def safe_exception(error: BaseException) -> list[dict]:
    """Code locations only: never inspect messages, source lines or local variables."""
    result = []
    for cause in exception_chain(error):
        frames = []
        for frame, line in walk_tb(cause.__traceback__):
            path = Path(frame.f_code.co_filename)
            try:
                filename = "keystone/" + path.relative_to(SOURCE_ROOT).as_posix()
            except ValueError:
                filename = path.name
            frames.append({"file": filename, "function": frame.f_code.co_name, "line": line})
        result.append({"type": type(cause).__name__, "frames": frames[-32:]})
    return result


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # Callers log safe event names, not request bodies, DSNs or exception text.
        value = {
            "time": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if getattr(record, "request_id", None):
            value["request_id"] = record.request_id
        # Only caller-built, explicitly whitelisted diagnostic fields belong here.
        if getattr(record, "diagnostic", None):
            value["diagnostic"] = record.diagnostic
        if getattr(record, "exception", None):
            value["exception"] = record.exception
        elif record.exc_info and record.exc_info[1]:
            value["exception"] = safe_exception(record.exc_info[1])
        return json.dumps(value, ensure_ascii=False)


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

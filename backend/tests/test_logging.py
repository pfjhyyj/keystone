import json
import logging
import sys

from keystone.core.logging import JsonFormatter, safe_exception


def test_json_exception_preserves_locations_and_chains_without_sensitive_values():
    def fail():
        private_local = "local-secret-never-log"
        raise ValueError("password=exception-secret-never-log " + private_local)

    try:
        try:
            fail()
        except ValueError as error:
            raise RuntimeError("request-body-secret-never-log") from error
    except RuntimeError as error:
        record = logging.LogRecord(
            "test", logging.ERROR, __file__, 1, "operation_failed", (), sys.exc_info()
        )
        record.request_id = "trusted-request-id"
        encoded = JsonFormatter().format(record)
        details = safe_exception(error)

    value = json.loads(encoded)
    assert value["request_id"] == "trusted-request-id"
    assert value["exception"] == details
    assert [item["type"] for item in details] == ["RuntimeError", "ValueError"]
    assert details[-1]["frames"][-1]["function"] == "fail"
    assert details[-1]["frames"][-1]["file"] == "test_logging.py"
    assert details[-1]["frames"][-1]["line"] > 0
    assert "secret-never-log" not in encoded
    assert "private_local" not in encoded


def test_exception_suppression_and_cycles_are_bounded():
    hidden = ValueError("hidden")
    outer = RuntimeError("safe")
    outer.__context__ = hidden
    outer.__suppress_context__ = True
    assert len(safe_exception(outer)) == 1
    outer.__cause__ = hidden
    hidden.__cause__ = outer
    assert len(safe_exception(outer)) == 2

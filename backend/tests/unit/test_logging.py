import json
import logging
import sys

from keystone.core.logging import JsonFormatter, exception_chain, safe_exception


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


def test_implicit_exception_context_and_deep_chains_are_bounded():
    try:
        try:
            raise OSError("private upstream URL and key")
        except OSError:
            raise RuntimeError("private body") from None
    except RuntimeError as suppressed:
        assert len(list(exception_chain(suppressed))) == 1
        suppressed.__suppress_context__ = False
        assert [type(error) for error in exception_chain(suppressed)] == [RuntimeError, OSError]

    error = ValueError("private")
    for _ in range(100):
        wrapper = RuntimeError("private")
        wrapper.__cause__ = error
        error = wrapper
    assert len(safe_exception(error)) == 8


def test_json_formatter_only_emits_explicit_diagnostics():
    record = logging.LogRecord("test", logging.ERROR, __file__, 1, "operation_failed", (), None)
    record.diagnostic = {"stage": "inference", "elapsed_ms": 25}
    record.raw_request = "must-never-log"
    encoded = JsonFormatter().format(record)
    assert json.loads(encoded)["diagnostic"] == record.diagnostic
    assert "must-never-log" not in encoded

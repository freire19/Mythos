"""Tests for alpha.jsonlogs — opt-in JSON logging."""

import json
import logging

from alpha.jsonlogs import JsonFormatter


def test_basic_record():
    fmt = JsonFormatter()
    record = logging.LogRecord(
        name="alpha.test", level=logging.INFO, pathname="x.py", lineno=10,
        msg="hello %s", args=("world",), exc_info=None,
    )
    out = json.loads(fmt.format(record))
    assert out["level"] == "INFO"
    assert out["logger"] == "alpha.test"
    assert out["msg"] == "hello world"
    assert "ts" in out


def test_extra_fields_flow_through():
    fmt = JsonFormatter()
    record = logging.LogRecord(
        name="alpha.test", level=logging.WARNING, pathname="x.py", lineno=1,
        msg="m", args=(), exc_info=None,
    )
    record.tool_name = "read_file"
    record.tokens_in = 1234
    out = json.loads(fmt.format(record))
    assert out["tool_name"] == "read_file"
    assert out["tokens_in"] == 1234


def test_non_serializable_extra_becomes_repr():
    fmt = JsonFormatter()
    record = logging.LogRecord(
        name="alpha.test", level=logging.INFO, pathname="x.py", lineno=1,
        msg="m", args=(), exc_info=None,
    )
    class _Thing:
        def __repr__(self): return "<Thing>"
    record.weird = _Thing()
    out = json.loads(fmt.format(record))
    assert out["weird"] == "<Thing>"


def test_exception_flattened():
    fmt = JsonFormatter()
    try:
        raise ValueError("boom")
    except ValueError:
        import sys
        exc_info = sys.exc_info()
    record = logging.LogRecord(
        name="alpha.test", level=logging.ERROR, pathname="x.py", lineno=1,
        msg="oops", args=(), exc_info=exc_info,
    )
    out = json.loads(fmt.format(record))
    assert "exc" in out
    assert "ValueError: boom" in out["exc"]

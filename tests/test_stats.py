"""Tests for alpha.stats — session telemetry."""

import time

import pytest

from alpha import stats


@pytest.fixture(autouse=True)
def _clear():
    stats.reset_session()
    yield
    stats.reset_session()


def test_records_iteration():
    stats.record_iteration()
    stats.record_iteration()
    s = stats.session_summary()
    assert s["iterations"] == 2


def test_records_tool_with_latency():
    stats.record_tool("read_file", 12.5)
    stats.record_tool("read_file", 7.5)
    stats.record_tool("write_file", 50.0)
    s = stats.session_summary()
    assert s["tool_calls_total"] == 3
    # Sorted by call count descending
    assert s["tools"][0]["name"] == "read_file"
    assert s["tools"][0]["calls"] == 2
    assert s["tools"][0]["avg_ms"] == 10.0
    assert s["tools"][1]["name"] == "write_file"
    assert s["tools"][1]["avg_ms"] == 50.0


def test_records_approval():
    stats.record_approval(True)
    stats.record_approval(False)
    stats.record_approval(True)
    s = stats.session_summary()
    assert s["approvals_required"] == 3
    assert s["approvals_granted"] == 2


def test_reset_clears():
    stats.record_iteration()
    stats.record_tool("x", 1.0)
    stats.record_approval(True)
    stats.reset_session()
    s = stats.session_summary()
    assert s["iterations"] == 0
    assert s["tool_calls_total"] == 0
    assert s["approvals_required"] == 0
    assert s["tools"] == []


def test_uptime_increases():
    s1 = stats.session_summary()
    time.sleep(0.01)
    s2 = stats.session_summary()
    assert s2["uptime_s"] >= s1["uptime_s"]

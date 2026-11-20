"""AUDIT_V2.2 #007: a sub-agent must not launch when the session cost cap is
already reached — otherwise each freshly-spawned sub-agent burns its first LLM
call before run_agent's per-iteration gate fires."""

import pytest

from alpha import cost
from alpha.tools.delegate_tools import _session_cap_skip


@pytest.fixture(autouse=True)
def _reset_session(monkeypatch):
    monkeypatch.delenv("ALPHA_MAX_SESSION_COST_USD", raising=False)
    cost._session = cost._SessionTotals()
    yield
    cost._session = cost._SessionTotals()


def test_no_cap_set_never_skips():
    assert _session_cap_skip("agent-1") is None


def test_under_cap_does_not_skip(monkeypatch):
    monkeypatch.setenv("ALPHA_MAX_SESSION_COST_USD", "5.00")
    assert _session_cap_skip("agent-1") is None


def test_at_or_over_cap_skips(monkeypatch):
    monkeypatch.setenv("ALPHA_MAX_SESSION_COST_USD", "0.01")
    # rack up cost well past the cap
    cost._session.add("openai", "gpt-4o", 5_000_000, 5_000_000)
    result = _session_cap_skip("agent-2")
    assert result is not None
    assert result["status"] == "skipped"
    assert result["ok"] is False
    assert result["category"] == "budget"
    assert result["agent_id"] == "agent-2"


def test_malformed_cap_is_ignored(monkeypatch):
    monkeypatch.setenv("ALPHA_MAX_SESSION_COST_USD", "not-a-number")
    cost._session.add("openai", "gpt-4o", 5_000_000, 5_000_000)
    assert _session_cap_skip("agent-3") is None

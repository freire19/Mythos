"""Regression tests for the DEEP_LOGIC wave-2 loop/core fixes.

Covers #DL202 (policy denials must not feed the error-streak), #DL207 /
#DL212 (delegate_* external timeout derived from the internal sub-agent
budgets), #DL211 (batch tool_results re-aligned to call order before the
loop-detector windows) and #DL213 (single session-cap parse helper shared
by the agent loop and delegate_tools).
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from alpha import cost
from alpha.agent import _ordered_batch_results, session_cost_cap
from alpha.agent.loop import _detect_error_streak, _result_is_failure
from alpha.tools.delegate_tools import _session_cap_skip


# ── #DL202: policy denials are not execution failures ──

class TestDeniedNotFailure:
    def test_denied_by_rule_result_is_not_failure(self):
        # Shape produced by executor._record_skip (deny-rule path).
        assert not _result_is_failure({
            "skipped": True, "reason": "blocked", "denied_by_rule": True,
            "ok": False, "category": "denied",
        })

    def test_user_denied_result_is_not_failure(self):
        assert not _result_is_failure({
            "skipped": True, "reason": "User denied this action",
            "ok": False, "category": "denied",
        })

    def test_hook_blocked_result_is_not_failure(self):
        assert not _result_is_failure({
            "skipped": True, "reason": "Hook blocked: nope",
            "hook_blocked": True, "ok": False, "category": "denied",
        })

    def test_real_failures_still_count(self):
        assert _result_is_failure({"ok": False, "category": "runtime", "error": "boom"})
        assert _result_is_failure({"ok": False, "category": "timeout", "error": "t"})
        assert _result_is_failure({"error": "self-reported"})
        assert _result_is_failure({"exit_code": 2, "stdout": "", "stderr": "x"})

    def test_streak_of_denials_never_reaches_threshold(self):
        denied = {"skipped": True, "ok": False, "category": "denied"}
        flags = [_result_is_failure(denied) for _ in range(6)]
        assert _detect_error_streak(flags) == 0

    async def test_executor_denial_shape_matches_exemption(self):
        """End-to-end: the result the executor emits for a deny-rule must be
        classified as not-a-failure by the loop detector."""
        from alpha.executor import execute_tool_calls

        async def _exec(**kwargs):
            return {"ok": True}

        tool_def = SimpleNamespace(executor=_exec, parameters={}, safety=None)
        events = []
        async for ev in execute_tool_calls(
            [{"id": "c1", "name": "execute_shell", "arguments": "{}"}],
            [],
            needs_approval_fn=lambda n, a: False,
            get_tool_fn=lambda n: tool_def,
            is_denied_fn=lambda n, a: (True, "deny rule"),
        ):
            events.append(ev)
        results = [e for e in events if e.get("type") == "tool_result"]
        assert len(results) == 1
        assert results[0].get("denied") is True
        assert results[0]["result"]["category"] == "denied"
        assert not _result_is_failure(results[0]["result"])


# ── #DL207 / #DL212: delegate_* timeout derived from internal budgets ──

class TestDelegateTimeout:
    def test_delegate_consensus_in_slow_tools(self):
        from alpha.executor import _SLOW_TOOLS
        assert "delegate_consensus" in _SLOW_TOOLS

    def test_consensus_timeout_exceeds_single_subagent_budget(self, monkeypatch):
        from alpha.config import FEATURES
        from alpha.executor import _delegate_timeout
        monkeypatch.setitem(FEATURES, "subagent_max_turn_seconds", 120.0)
        monkeypatch.setitem(FEATURES, "max_parallel_agents", 3)
        assert _delegate_timeout("delegate_consensus", {"n": 3}) > 120.0

    def test_full_parallel_batch_covers_all_waves(self, monkeypatch):
        from alpha.config import FEATURES
        from alpha.executor import _delegate_timeout
        monkeypatch.setitem(FEATURES, "subagent_max_turn_seconds", 120.0)
        monkeypatch.setitem(FEATURES, "max_parallel_agents", 3)
        monkeypatch.setitem(FEATURES, "max_delegate_total_tasks", 10)
        tasks = json.dumps([f"t{i}" for i in range(10)])
        # 10 tasks / 3 concurrent = 4 waves x 120s each.
        assert _delegate_timeout("delegate_parallel", {"tasks": tasks}) >= 4 * 120.0

    def test_malformed_tasks_falls_back_to_slow_timeout(self):
        from alpha.executor import _SLOW_TOOL_TIMEOUT, _delegate_timeout
        assert _delegate_timeout("delegate_parallel", {"tasks": "not json"}) >= _SLOW_TOOL_TIMEOUT
        assert _delegate_timeout("delegate_task", {}) >= _SLOW_TOOL_TIMEOUT

    async def test_execute_single_tool_applies_derived_timeout(self, monkeypatch):
        from alpha import executor
        from alpha.config import FEATURES
        monkeypatch.setitem(FEATURES, "subagent_max_turn_seconds", 120.0)
        monkeypatch.setitem(FEATURES, "max_parallel_agents", 3)
        monkeypatch.setitem(FEATURES, "max_delegate_total_tasks", 10)

        captured = {}
        orig_wait_for = asyncio.wait_for

        async def fake_wait_for(coro, timeout=None):
            captured["timeout"] = timeout
            return await orig_wait_for(coro, timeout=timeout)

        monkeypatch.setattr(executor.asyncio, "wait_for", fake_wait_for)

        async def _exec(**kwargs):
            return {"ok": True}

        tool_def = SimpleNamespace(executor=_exec)
        tasks = json.dumps([f"t{i}" for i in range(10)])
        await executor._execute_single_tool(
            tool_def, "delegate_parallel", {"tasks": tasks}
        )
        assert captured["timeout"] >= 4 * 120.0


# ── #DL211: batch results re-aligned to call order ──

class TestOrderedBatchResults:
    def test_reorders_by_call_index(self):
        tcs = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        arrived = [("c", {"v": 3}), ("a", {"v": 1}), ("b", {"v": 2})]
        assert _ordered_batch_results(tcs, arrived) == [{"v": 1}, {"v": 2}, {"v": 3}]

    def test_unknown_ids_go_last_in_arrival_order(self):
        tcs = [{"id": "a"}]
        arrived = [("x", {"v": "x"}), ("a", {"v": "a"}), ("y", {"v": "y"})]
        assert _ordered_batch_results(tcs, arrived) == [
            {"v": "a"}, {"v": "x"}, {"v": "y"},
        ]

    def test_missing_ids_keep_stable_order(self):
        tcs = [{"id": ""}, {"id": ""}]
        arrived = [("", {"v": 1}), ("", {"v": 2})]
        assert _ordered_batch_results(tcs, arrived) == [{"v": 1}, {"v": 2}]

    async def test_mixed_batch_realigned_after_grouped_yields(self):
        """A batch [ok, denied] is yielded grouped (denied first) by the
        executor; the reorder restores call order for the loop windows."""
        from alpha.executor import execute_tool_calls

        async def _exec(**kwargs):
            return {"ok": True, "value": "fine"}

        tool_def = SimpleNamespace(executor=_exec, parameters={}, safety=None)
        tcs = [
            {"id": "call_ok", "name": "tool_ok", "arguments": "{}"},
            {"id": "call_deny", "name": "tool_deny", "arguments": "{}"},
        ]
        batch: list[tuple[str, dict]] = []
        async for ev in execute_tool_calls(
            tcs,
            [],
            needs_approval_fn=lambda n, a: False,
            get_tool_fn=lambda n: tool_def,
            is_denied_fn=lambda n, a: (n == "tool_deny", "deny rule"),
        ):
            if ev.get("type") == "tool_result":
                batch.append((str(ev.get("id") or ""), ev.get("result", {})))

        # Grouped yield order: denial before the gather success.
        assert [b[0] for b in batch] == ["call_deny", "call_ok"]
        ordered = _ordered_batch_results(tcs, batch)
        assert ordered[0].get("value") == "fine"
        assert ordered[1].get("category") == "denied"


# ── #DL213: single session-cap parse shared between the two gates ──

@pytest.fixture()
def _reset_session(monkeypatch):
    monkeypatch.delenv("ALPHA_MAX_SESSION_COST_USD", raising=False)
    cost._session = cost._SessionTotals()
    yield
    cost._session = cost._SessionTotals()


class TestSessionCostCap:
    def test_unset_returns_none(self, _reset_session):
        assert session_cost_cap() is None

    def test_parses_with_strip(self, _reset_session, monkeypatch):
        monkeypatch.setenv("ALPHA_MAX_SESSION_COST_USD", "  1.5  ")
        assert session_cost_cap() == 1.5

    @pytest.mark.parametrize("raw", ["nan", "inf", "-3", "abc", "   "])
    def test_invalid_values_ignored(self, _reset_session, monkeypatch, raw):
        monkeypatch.setenv("ALPHA_MAX_SESSION_COST_USD", raw)
        assert session_cost_cap() is None

    def test_skip_ignores_nan_cap(self, _reset_session, monkeypatch):
        # Old raw-float parse accepted nan → `spent < nan` False → bogus skip.
        monkeypatch.setenv("ALPHA_MAX_SESSION_COST_USD", "nan")
        cost._session.add("openai", "gpt-4o", 5_000_000, 5_000_000)
        assert _session_cap_skip("agent-nan") is None

    def test_skip_blocks_at_exact_cap_boundary(self, _reset_session, monkeypatch):
        # `spent < cap → ok` in both gates: at the exact cap it blocks.
        monkeypatch.setenv("ALPHA_MAX_SESSION_COST_USD", "0.0")
        result = _session_cap_skip("agent-boundary")
        assert result is not None
        assert result["status"] == "skipped"
        assert result["category"] == "budget"

    def test_skip_still_skips_over_cap(self, _reset_session, monkeypatch):
        monkeypatch.setenv("ALPHA_MAX_SESSION_COST_USD", "0.01")
        cost._session.add("openai", "gpt-4o", 5_000_000, 5_000_000)
        result = _session_cap_skip("agent-over")
        assert result is not None
        assert result["ok"] is False

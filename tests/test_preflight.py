"""Tests for the pre-flight feature (RFC docs/specs/pre-flight-cards.md).

Slice 1 coverage:
- Cost estimator: known-model price applied, unknown-model → $0
- Time estimator: per-tool defaults + fallback + LLM round-trip overhead
- pre_flight tool executor: validates inputs, returns enriched card,
  honors ALPHA_MAX_TURN_COST_USD budget cap
- Card renderer: smoke test that the printer doesn't crash on the
  standard shapes (rendering correctness is visual, not unit-testable)
"""

from __future__ import annotations

import pytest

from alpha.preflight import (
    estimate_step_cost,
    estimate_step_time,
    estimate_total_cost,
    estimate_total_time,
)
from alpha.tools.plan_tools import _pre_flight


# ─── cost_estimate ─────────────────────────────────────────────────


class TestCostEstimate:
    def test_known_model_produces_positive_estimate(self):
        cost = estimate_step_cost(
            tool="read_file",
            args_preview="alpha/agent/__init__.py",
            model="deepseek-chat",
        )
        assert cost > 0
        # Sanity check the magnitude — a single read_file shouldn't be
        # more than half a cent on deepseek.
        assert cost < 0.005

    def test_unknown_model_returns_zero(self):
        cost = estimate_step_cost(
            tool="read_file",
            args_preview="anything",
            model="totally-fake-model-xyz",
        )
        assert cost == 0.0

    def test_empty_model_returns_zero(self):
        cost = estimate_step_cost("read_file", "anything", "")
        assert cost == 0.0

    def test_expensive_tool_costs_more_than_cheap_tool(self):
        cheap = estimate_step_cost("read_file", "a.py", "claude-opus-4-7")
        expensive = estimate_step_cost(
            "delegate_parallel", "task=audit security", "claude-opus-4-7"
        )
        # delegate_parallel expected output (3000 tokens) is 15x read_file
        # (200 tokens). Output dominates on opus pricing, so expensive
        # should clearly outpace cheap.
        assert expensive > cheap * 5

    def test_total_sums_steps(self):
        steps = [
            {"tool": "read_file", "args_preview": "a.py"},
            {"tool": "read_file", "args_preview": "b.py"},
            {"tool": "write_file", "args_preview": "c.py"},
        ]
        total = estimate_total_cost(steps, "deepseek-chat")
        individual = sum(
            estimate_step_cost(s["tool"], s["args_preview"], "deepseek-chat")
            for s in steps
        )
        assert total == pytest.approx(individual)


# ─── time_estimate ─────────────────────────────────────────────────


class TestTimeEstimate:
    def test_known_tool_uses_default(self):
        assert estimate_step_time("execute_python") == 5.0

    def test_unknown_tool_falls_back(self):
        # The fallback constant is the only signal; testing the exact
        # value would couple the test to an arbitrary number, so just
        # confirm it returned something positive and not the
        # execute_python default.
        t = estimate_step_time("totally_unknown_tool")
        assert t > 0
        assert t != 5.0

    def test_total_includes_llm_overhead(self):
        # A 3-step plan of cheap tools is dominated by the LLM round-trip
        # overhead (~3s each) not the tool wall-clock.
        steps = [{"tool": "read_file"}, {"tool": "read_file"}, {"tool": "read_file"}]
        total = estimate_total_time(steps)
        tool_only = 3 * 0.1
        assert total > tool_only + 8  # at least 9s of LLM overhead


# ─── _pre_flight executor ─────────────────────────────────────────


class TestPreFlightExecutor:
    @pytest.mark.asyncio
    async def test_happy_path_returns_enriched_card(self, monkeypatch):
        monkeypatch.delenv("ALPHA_MAX_TURN_COST_USD", raising=False)
        result = await _pre_flight(
            goal="Fix the memory leak in executor.py",
            steps=[
                {"tool": "read_file", "args_preview": "alpha/executor.py", "why": "see current code"},
                {"tool": "edit_file", "args_preview": "alpha/executor.py", "why": "patch leak"},
            ],
            confidence="medium",
            alternatives_rejected=[
                {"approach": "rewrite from scratch", "why_rejected": "scope creep"},
            ],
            model="deepseek-chat",
        )
        assert result["approved"] is True
        assert result["goal"] == "Fix the memory leak in executor.py"
        assert len(result["steps"]) == 2
        assert result["confidence"] == "medium"
        assert result["estimated_cost_usd"] > 0
        assert result["estimated_time_s"] > 0
        assert len(result["alternatives_rejected"]) == 1

    @pytest.mark.asyncio
    async def test_rejects_empty_goal(self):
        result = await _pre_flight(goal="", steps=[{"tool": "x", "args_preview": "y"}], confidence="high")
        assert "error" in result
        assert "goal" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_rejects_empty_steps(self):
        result = await _pre_flight(goal="x", steps=[], confidence="high")
        assert "error" in result
        assert "step" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_rejects_invalid_confidence(self):
        result = await _pre_flight(
            goal="x",
            steps=[{"tool": "y", "args_preview": "z"}],
            confidence="kinda-sure",
        )
        assert "error" in result
        assert "confidence" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_rejects_step_missing_tool(self):
        result = await _pre_flight(
            goal="x",
            steps=[{"args_preview": "y"}],  # no 'tool'
            confidence="high",
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_budget_cap_blocks_when_over(self, monkeypatch):
        # Set a cap so low that any non-zero estimate exceeds it.
        monkeypatch.setenv("ALPHA_MAX_TURN_COST_USD", "0.00001")
        result = await _pre_flight(
            goal="anything",
            steps=[{"tool": "delegate_parallel", "args_preview": "expensive task"}],
            confidence="high",
            model="claude-opus-4-7",  # expensive model amplifies estimate
        )
        assert result.get("error") == "budget_cap_exceeded"
        assert "estimated_cost_usd" in result
        assert result["cap_usd"] == 0.00001

    @pytest.mark.asyncio
    async def test_budget_cap_passes_when_under(self, monkeypatch):
        monkeypatch.setenv("ALPHA_MAX_TURN_COST_USD", "100.0")
        result = await _pre_flight(
            goal="anything",
            steps=[{"tool": "read_file", "args_preview": "x.py"}],
            confidence="high",
            model="deepseek-chat",
        )
        assert result.get("approved") is True

    @pytest.mark.asyncio
    async def test_no_cap_set_never_blocks(self, monkeypatch):
        monkeypatch.delenv("ALPHA_MAX_TURN_COST_USD", raising=False)
        result = await _pre_flight(
            goal="anything",
            steps=[{"tool": "delegate_parallel", "args_preview": "x"}],
            confidence="high",
            model="claude-opus-4-7",
        )
        assert result.get("approved") is True

    @pytest.mark.asyncio
    async def test_malformed_cap_is_ignored(self, monkeypatch):
        """A garbage cap value shouldn't crash — silently ignored."""
        monkeypatch.setenv("ALPHA_MAX_TURN_COST_USD", "not-a-number")
        result = await _pre_flight(
            goal="x",
            steps=[{"tool": "read_file", "args_preview": "y"}],
            confidence="high",
        )
        assert result.get("approved") is True


# ─── renderer smoke test ──────────────────────────────────────────


class TestPreFlightRenderer:
    def test_renderer_does_not_crash_on_minimal_card(self, capsys):
        from alpha.display.renderers.planning import _print_preflight_card

        _print_preflight_card({
            "goal": "test",
            "steps": [{"tool": "read_file", "args_preview": "x.py"}],
            "confidence": "high",
            "model": "deepseek-chat",
        })
        out = capsys.readouterr().out
        assert "PRE-FLIGHT" in out
        assert "test" in out
        assert "read_file" in out

    def test_renderer_includes_alternatives(self, capsys):
        from alpha.display.renderers.planning import _print_preflight_card

        _print_preflight_card({
            "goal": "x",
            "steps": [{"tool": "read_file", "args_preview": "a"}],
            "confidence": "low",
            "alternatives_rejected": [
                {"approach": "alt one", "why_rejected": "too expensive"},
            ],
            "model": "deepseek-chat",
        })
        out = capsys.readouterr().out
        assert "alt one" in out
        assert "too expensive" in out

    def test_renderer_handles_missing_optional_fields(self, capsys):
        from alpha.display.renderers.planning import _print_preflight_card

        # No model, no alternatives, no `why` per step.
        _print_preflight_card({
            "goal": "x",
            "steps": [{"tool": "read_file", "args_preview": "a"}],
            "confidence": "high",
        })
        out = capsys.readouterr().out
        assert "PRE-FLIGHT" in out

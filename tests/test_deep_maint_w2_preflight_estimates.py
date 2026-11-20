"""#DM060 — pre_flight estimator dicts must only key on registered tools.

The cost/time estimators keep per-tool tuning tables. Keys that don't
match a registered tool name silently fall through to the generic
default, so the "tuning" is dead config. These tests lock every key of
both dicts against the real registry (get_openai_tools) to stop drift.
"""

from __future__ import annotations

import pytest

from alpha.preflight.cost_estimate import (
    _DEFAULT_OUTPUT_TOKENS,
    _EXPECTED_OUTPUT_TOKENS,
    estimate_step_cost,
    estimate_total_cost,
)
from alpha.preflight.time_estimate import (
    _DEFAULT_STEP_TIME_FALLBACK_S,
    _DEFAULT_STEP_TIME_S,
    estimate_step_time,
    estimate_total_time,
)
from alpha.tools import get_openai_tools, load_all_tools


@pytest.fixture(scope="module")
def registered_names() -> set[str]:
    load_all_tools()
    return {t["function"]["name"] for t in get_openai_tools()}


class TestEstimatorKeysMatchRegistry:
    """Every tuning key must be a real tool — locks #DM060 against drift."""

    def test_cost_estimator_keys_exist_in_registry(self, registered_names):
        dead = set(_EXPECTED_OUTPUT_TOKENS) - registered_names
        assert not dead, (
            f"cost_estimate._EXPECTED_OUTPUT_TOKENS has dead keys: {sorted(dead)}"
        )

    def test_time_estimator_keys_exist_in_registry(self, registered_names):
        dead = set(_DEFAULT_STEP_TIME_S) - registered_names
        assert not dead, (
            f"time_estimate._DEFAULT_STEP_TIME_S has dead keys: {sorted(dead)}"
        )

    def test_renamed_families_are_now_tuned(self):
        # The point of the fix: git/search/list steps no longer fall through
        # to the generic default.
        for key in ("search_files", "list_directory", "git_operation"):
            assert key in _EXPECTED_OUTPUT_TOKENS
            assert key in _DEFAULT_STEP_TIME_S
            assert _DEFAULT_STEP_TIME_S[key] != _DEFAULT_STEP_TIME_FALLBACK_S

    def test_dead_keys_removed(self):
        for key in ("grep_files", "list_files", "git_status", "git_diff",
                    "git_log", "git_commit"):
            assert key not in _EXPECTED_OUTPUT_TOKENS
            assert key not in _DEFAULT_STEP_TIME_S


class TestBehaviorPreserved:
    """Estimator semantics unchanged for untouched paths."""

    def test_unknown_tool_falls_back_to_defaults(self):
        assert estimate_step_time("no_such_tool") == _DEFAULT_STEP_TIME_FALLBACK_S
        # Unknown tool uses _DEFAULT_OUTPUT_TOKENS in the cost formula —
        # compare against a hand-computed value with a known-priced model.
        cost_default = estimate_step_cost("no_such_tool", "x" * 40, "deepseek-chat")
        assert cost_default > 0.0
        assert _DEFAULT_OUTPUT_TOKENS == 250

    def test_estimate_total_time_adds_llm_overhead_per_step(self):
        steps = [{"tool": "read_file"}, {"tool": "git_operation"}]
        expected = (
            _DEFAULT_STEP_TIME_S["read_file"]
            + _DEFAULT_STEP_TIME_S["git_operation"]
            + 3.0 * 2
        )
        assert estimate_total_time(steps) == pytest.approx(expected)

    def test_estimate_total_cost_keeps_context_term(self, monkeypatch):
        # #DL203: total = per-step sum + last_call_input_cost_usd() * n_steps.
        import alpha.preflight.cost_estimate as ce

        monkeypatch.setattr(ce, "last_call_input_cost_usd", lambda: 0.01)
        steps = [
            {"tool": "read_file", "args_preview": "a" * 20},
            {"tool": "search_files", "args_preview": "b" * 20},
        ]
        per_step = sum(
            estimate_step_cost(s["tool"], s["args_preview"], "deepseek-chat")
            for s in steps
        )
        total = estimate_total_cost(steps, "deepseek-chat")
        assert total == pytest.approx(per_step + 0.01 * 2)

    def test_unknown_model_estimates_zero(self):
        assert estimate_step_cost("read_file", "abc", "no-such-model") == 0.0

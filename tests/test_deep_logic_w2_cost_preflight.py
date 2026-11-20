"""Regression tests for the DEEP_LOGIC wave-2 cost/pre-flight fixes.

Covers #DL203 (estimativa do gate por-turno inclui o contexto acumulado
reenviado a cada turno) e #DL214 (chaves exatas em _PRICING para o tier
grok-4-1-fast e demais default_model de config.py).
"""
from __future__ import annotations

import pytest

from alpha import cost
from alpha.preflight import estimate_step_cost, estimate_total_cost


@pytest.fixture(autouse=True)
def _clear_session():
    cost.reset_session()
    yield
    cost.reset_session()


# ── #DL203: gate estimate includes the accumulated-context term ──


class TestContextTermInGateEstimate:
    STEPS = [
        {"tool": "read_file", "args_preview": "a.py"},
        {"tool": "write_file", "args_preview": "b.py"},
    ]

    def test_no_recorded_call_matches_per_step_sum(self):
        # Session start: no context recorded yet → old behavior preserved.
        total = estimate_total_cost(self.STEPS, "gpt-4o")
        per_step = sum(
            estimate_step_cost(s["tool"], s["args_preview"], "gpt-4o")
            for s in self.STEPS
        )
        assert total == pytest.approx(per_step)

    def test_context_term_added_once_per_step(self):
        per_step = sum(
            estimate_step_cost(s["tool"], s["args_preview"], "gpt-4o")
            for s in self.STEPS
        )
        # 150K input tokens on gpt-4o (2.50/M) → $0.375 of context per turn.
        cost.record_usage(
            "openai", "gpt-4o", {"prompt_tokens": 150_000, "completion_tokens": 10}
        )
        total = estimate_total_cost(self.STEPS, "gpt-4o")
        expected_ctx = 150_000 * 2.50 / 1_000_000
        assert total == pytest.approx(per_step + expected_ctx * len(self.STEPS))
        # The context term dominates the args-only estimate by orders of
        # magnitude — the whole point of #DL203.
        assert total > per_step * 10

    def test_last_call_wins_not_session_total(self):
        cost.record_usage(
            "openai", "gpt-4o", {"prompt_tokens": 100_000, "completion_tokens": 10}
        )
        cost.record_usage(
            "openai", "gpt-4o", {"prompt_tokens": 20_000, "completion_tokens": 10}
        )
        # Context re-sent next turn ≈ last call's input, not the 120K sum.
        assert cost.last_call_input_cost_usd() == pytest.approx(
            20_000 * 2.50 / 1_000_000
        )

    def test_cache_tokens_priced_with_multipliers(self):
        cost.record_usage(
            "anthropic",
            "claude-sonnet-4-6",
            {
                "input_tokens": 1_000,
                "output_tokens": 10,
                "cache_read_input_tokens": 100_000,
                "cache_creation_input_tokens": 2_000,
            },
        )
        p_in = 3.00
        expected = (
            1_000 * p_in + 100_000 * p_in * 0.1 + 2_000 * p_in * 1.25
        ) / 1_000_000
        assert cost.last_call_input_cost_usd() == pytest.approx(expected)

    def test_reset_session_clears_last_call(self):
        cost.record_usage(
            "openai", "gpt-4o", {"prompt_tokens": 50_000, "completion_tokens": 10}
        )
        assert cost.last_call_input_cost_usd() > 0
        cost.reset_session()
        assert cost.last_call_input_cost_usd() == 0.0

    def test_card_step_estimate_stays_context_free(self):
        # pre_flight card decoration (estimate_step_cost) must NOT absorb
        # the context term — only the gate path (estimate_total_cost) does.
        before = estimate_step_cost("read_file", "a.py", "gpt-4o")
        cost.record_usage(
            "openai", "gpt-4o", {"prompt_tokens": 150_000, "completion_tokens": 10}
        )
        after = estimate_step_cost("read_file", "a.py", "gpt-4o")
        assert after == pytest.approx(before)


# ── #DL214: exact pricing keys for provider default models ──


class TestGrokFastPricing:
    def test_grok_fast_reasoning_exact_key(self):
        # Preço oficial do tier 4.1-fast: $0.20/M in, $0.50/M out — não os
        # (3.00, 15.00) do grok-4 cheio que o prefix-match aplicava.
        assert cost._price_for("grok-4-1-fast-reasoning") == (0.20, 0.50)

    def test_grok_fast_non_reasoning_exact_key(self):
        assert cost._price_for("grok-4-1-fast-non-reasoning") == (0.20, 0.50)

    def test_grok_fast_variant_boundary_matches_fast_tier(self):
        # Sufixos futuros do tier fast devem cair no preço fast, não no full.
        assert cost._price_for("grok-4-1-fast-latest") == (0.20, 0.50)

    def test_grok_4_full_price_unchanged(self):
        assert cost._price_for("grok-4") == (3.00, 15.00)

    def test_every_paid_default_model_has_exact_pricing_entry(self):
        from alpha.config import _PROVIDERS

        # Ollama-backed providers are local/free — the (0,0) fallback is the
        # correct billing for them, so they are exempt from exact keys.
        local = {"ollama", "gemma-12b", "gemma-27b"}
        for name, cfg in _PROVIDERS.items():
            if name in local:
                continue
            model = cfg["default_model"].lower()
            assert model in cost._PRICING, (
                f"default_model {model!r} do provider {name!r} sem chave "
                "exata em _PRICING (#DL214/#200)"
            )

"""Tests for alpha.cost — session token + USD tracking."""

import pytest

from alpha import cost


@pytest.fixture(autouse=True)
def _clear_session():
    cost.reset_session()
    yield
    cost.reset_session()


class TestPriceFor:
    def test_known_model(self):
        # gpt-4o is in the table verbatim.
        p_in, p_out = cost._price_for("gpt-4o")
        assert p_in == 2.50
        assert p_out == 10.00

    def test_substring_match(self):
        # Provider-specific suffix should still resolve to the base price.
        p_in, p_out = cost._price_for("gemini-3.1-pro-preview-customtools")
        assert p_in == 1.25
        assert p_out == 10.00

    def test_longest_key_wins(self):
        # claude-sonnet-4-6 should match the specific entry, not claude-sonnet-4.
        # Both happen to be priced the same here but the lookup must still pick
        # the longer key.
        p = cost._price_for("claude-sonnet-4-6-1m")
        assert p == (3.00, 15.00)

    def test_unknown_model_returns_zero(self):
        assert cost._price_for("totally-made-up-model-9000") == (0.0, 0.0)

    def test_empty_string(self):
        assert cost._price_for("") == (0.0, 0.0)


class TestRecordUsage:
    def test_records_openai_style(self):
        cost.record_usage(
            "openai",
            "gpt-4o-mini",
            {"prompt_tokens": 1000, "completion_tokens": 200},
        )
        s = cost.session_summary()
        assert s["calls"] == 1
        assert s["tokens_in"] == 1000
        assert s["tokens_out"] == 200
        # 1000 * 0.15/1M + 200 * 0.60/1M = 0.00015 + 0.00012 = 0.00027
        assert s["cost_usd"] == pytest.approx(0.00027, abs=1e-9)

    def test_records_anthropic_style(self):
        cost.record_usage(
            "anthropic",
            "claude-haiku-4-5",
            {"input_tokens": 5000, "output_tokens": 500},
        )
        s = cost.session_summary()
        assert s["calls"] == 1
        assert s["tokens_in"] == 5000
        assert s["tokens_out"] == 500

    def test_aggregates_across_calls_same_model(self):
        cost.record_usage("openai", "gpt-4o-mini", {"prompt_tokens": 100, "completion_tokens": 50})
        cost.record_usage("openai", "gpt-4o-mini", {"prompt_tokens": 200, "completion_tokens": 75})
        s = cost.session_summary()
        assert s["calls"] == 2
        assert s["tokens_in"] == 300
        assert s["tokens_out"] == 125
        # One row in by_model
        assert len(s["by_model"]) == 1

    def test_separate_rows_per_model(self):
        cost.record_usage("openai", "gpt-4o-mini", {"prompt_tokens": 100, "completion_tokens": 50})
        cost.record_usage("anthropic", "claude-sonnet-4-6", {"input_tokens": 100, "output_tokens": 50})
        s = cost.session_summary()
        assert s["calls"] == 2
        assert len(s["by_model"]) == 2

    def test_unknown_model_tracks_tokens_zero_cost(self):
        cost.record_usage(
            "fake",
            "made-up-model",
            {"prompt_tokens": 1000, "completion_tokens": 500},
        )
        s = cost.session_summary()
        assert s["tokens_in"] == 1000
        assert s["tokens_out"] == 500
        assert s["cost_usd"] == 0.0

    def test_none_usage_is_noop(self):
        cost.record_usage("openai", "gpt-4o", None)
        assert cost.session_summary()["calls"] == 0

    def test_empty_usage_is_noop(self):
        cost.record_usage("openai", "gpt-4o", {})
        assert cost.session_summary()["calls"] == 0

    def test_malformed_usage_is_noop(self):
        cost.record_usage("openai", "gpt-4o", "not-a-dict")  # type: ignore[arg-type]
        assert cost.session_summary()["calls"] == 0


class TestReset:
    def test_reset_clears_everything(self):
        cost.record_usage("openai", "gpt-4o", {"prompt_tokens": 100, "completion_tokens": 50})
        assert cost.session_summary()["calls"] == 1
        cost.reset_session()
        s = cost.session_summary()
        assert s["calls"] == 0
        assert s["tokens_in"] == 0
        assert s["cost_usd"] == 0.0
        assert s["by_model"] == []


class TestFormatSessionLine:
    def test_empty_session(self):
        assert cost.format_session_line() == "no LLM calls this session"

    def test_with_calls(self):
        cost.record_usage("openai", "gpt-4o-mini", {"prompt_tokens": 1000, "completion_tokens": 500})
        line = cost.format_session_line()
        assert "1 call(s)" in line
        assert "1,000 in" in line
        assert "500 out" in line
        assert "$" in line

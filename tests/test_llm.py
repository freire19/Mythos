"""Tests for LLM module — retry logic and backoff calculation.

Backoff applies full jitter (50%-100% of base on the exponential path,
80%-120% on the retry_after path) to avoid thundering-herd retries.
Tests assert the expected jitter window, not exact values.
"""

import statistics

from alpha.config import RETRY
from alpha.llm import _build_request, _calc_backoff

INITIAL_BACKOFF = RETRY["llm"]["initial_backoff"]
MAX_BACKOFF = RETRY["llm"]["max_backoff"]
MAX_RETRIES = RETRY["llm"]["max_retries"]


class TestBackoff:
    def test_first_attempt_within_jitter_window(self):
        # Exponential path: base = INITIAL_BACKOFF, jittered to [0.5*base, 1.0*base]
        for _ in range(50):
            delay = _calc_backoff(0)
            assert 0.5 * INITIAL_BACKOFF <= delay <= INITIAL_BACKOFF

    def test_exponential_growth_on_average(self):
        # With jitter individual samples can invert; compare medians.
        samples_0 = [_calc_backoff(0) for _ in range(100)]
        samples_1 = [_calc_backoff(1) for _ in range(100)]
        samples_2 = [_calc_backoff(2) for _ in range(100)]
        assert statistics.median(samples_1) > statistics.median(samples_0)
        assert statistics.median(samples_2) > statistics.median(samples_1)

    def test_capped_at_max(self):
        # Large attempt → exponential blows up → cap dominates and wins out.
        for _ in range(50):
            delay = _calc_backoff(100)
            assert delay <= MAX_BACKOFF

    def test_retry_after_header_respected(self):
        # retry_after path: returned within ±20% of header value.
        for _ in range(50):
            delay = _calc_backoff(0, retry_after=10.0)
            assert 8.0 <= delay <= 12.0

    def test_retry_after_capped(self):
        # Header above MAX_BACKOFF gets clamped before jitter is applied.
        # Window: [MAX_BACKOFF*0.8, MAX_BACKOFF*1.2]
        for _ in range(50):
            delay = _calc_backoff(0, retry_after=999.0)
            assert MAX_BACKOFF * 0.8 <= delay <= MAX_BACKOFF * 1.2

    def test_max_retries_is_3(self):
        assert MAX_RETRIES == 3


class TestBuildRequestExtraPayload:
    """_build_request must forward provider-specific fields (e.g. DeepSeek's
    thinking/reasoning_effort) into the OpenAI-compat POST body."""

    def _common(self):
        return {
            "messages": [{"role": "user", "content": "oi"}],
            "tools": [],
            "temperature": 0.5,
            "api_key": "k",
            "model": "deepseek-v4-pro",
            "supports_tools": True,
        }

    def test_extra_payload_merged_into_body(self):
        _, payload = _build_request(
            **self._common(),
            extra_payload={
                "thinking": {"type": "enabled"},
                "reasoning_effort": "max",
            },
        )
        assert payload["thinking"] == {"type": "enabled"}
        assert payload["reasoning_effort"] == "max"
        # base fields preserved
        assert payload["model"] == "deepseek-v4-pro"
        assert payload["stream"] is True

    def test_no_extra_payload_keeps_body_clean(self):
        _, payload = _build_request(**self._common())
        assert "thinking" not in payload
        assert "reasoning_effort" not in payload

    def test_empty_extra_payload_is_noop(self):
        _, payload = _build_request(**self._common(), extra_payload={})
        assert "thinking" not in payload

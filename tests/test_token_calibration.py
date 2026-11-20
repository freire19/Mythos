"""Tier 1: calibrate the char-based token heuristic against the provider's
real input-token count so the compression trigger is accurate."""

import pytest

from alpha import context as ctx


@pytest.fixture(autouse=True)
def _reset():
    ctx._token_calibration.set(1.0)
    ctx._token_estimate_cache.set(None)
    yield
    ctx._token_calibration.set(1.0)
    ctx._token_estimate_cache.set(None)


class TestInputTokensFromUsage:
    def test_openai_shape(self):
        assert ctx.input_tokens_from_usage({"prompt_tokens": 1234}) == 1234

    def test_anthropic_sums_cache(self):
        # input_tokens EXCLUDES cache; real context = input + cache_read + creation.
        usage = {"input_tokens": 100, "cache_read_input_tokens": 2000,
                 "cache_creation_input_tokens": 500}
        assert ctx.input_tokens_from_usage(usage) == 2600

    def test_none_when_absent(self):
        assert ctx.input_tokens_from_usage({}) is None
        assert ctx.input_tokens_from_usage(None) is None


class TestCalibration:
    def test_underestimate_pushes_ratio_up(self):
        # Heuristic said 1000, provider says 2000 → future estimates scale up.
        ctx.record_real_token_usage(1000, {"input_tokens": 2000})
        assert ctx._token_calibration.get() > 1.0

    def test_ratio_is_clamped(self):
        # A wild 100x real/estimate can't blow the factor past the ceiling.
        for _ in range(20):
            ctx.record_real_token_usage(10, {"input_tokens": 100_000})
        assert ctx._token_calibration.get() <= ctx._CALIBRATION_MAX

    def test_noop_on_missing_usage(self):
        ctx.record_real_token_usage(1000, None)
        assert ctx._token_calibration.get() == 1.0

    def test_noop_on_zero_estimate(self):
        ctx.record_real_token_usage(0, {"input_tokens": 500})
        assert ctx._token_calibration.get() == 1.0

    def test_calibrated_scales_estimate(self):
        msgs = [{"role": "user", "content": "x" * 4000}]  # ~1000 est tokens
        base = ctx.estimate_messages_tokens(msgs)
        ctx._token_calibration.set(2.0)
        ctx._token_estimate_cache.set(None)
        assert ctx.calibrated_messages_tokens(msgs) == pytest.approx(base * 2, abs=2)

    def test_needs_compression_uses_calibration(self):
        # Build a history under the raw threshold but over it once calibrated 2x.
        limit = ctx.get_context_limit("anthropic")
        # target ~ 40% of limit in raw chars so raw is under 0.70 but 2x is over
        chars = int(limit * 0.40) * ctx.CHARS_PER_TOKEN
        msgs = [{"role": "system", "content": "s"}]
        msgs += [{"role": "user", "content": "y"} for _ in range(ctx.MIN_MESSAGES_FOR_COMPRESSION)]
        msgs.append({"role": "user", "content": "x" * chars})
        ctx._token_estimate_cache.set(None)
        assert ctx.needs_compression(msgs, "anthropic") is False
        ctx._token_calibration.set(2.0)
        ctx._token_estimate_cache.set(None)
        assert ctx.needs_compression(msgs, "anthropic") is True

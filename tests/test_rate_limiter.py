"""TokenBucket throttling correctness.

Regression test for AUDIT_V2.1 #003: after a waiter sleeps `wait` for a
token it set `last_refill = now` (pre-sleep), so the NEXT acquire credited
the slept interval AGAIN — granting a free token and under-throttling ~2x.
The fix advances `last_refill = now + wait`.
"""
from __future__ import annotations

import pytest

import alpha._rate_limiter as rl


class _Clock:
    def __init__(self, t0: float = 1000.0):
        self.t = t0

    def monotonic(self) -> float:
        return self.t


@pytest.mark.asyncio
async def test_token_bucket_does_not_double_count_slept_interval(monkeypatch):
    clock = _Clock()
    sleeps: list[float] = []

    async def fake_sleep(d):
        sleeps.append(d)
        clock.t += d  # advance the deterministic clock by the slept time

    monkeypatch.setattr(rl.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(rl.asyncio, "sleep", fake_sleep)

    bucket = rl.TokenBucket(rate=1.0, burst=1)  # 1 token/sec, burst 1

    # 1st acquire consumes the burst token immediately — no wait.
    await bucket.acquire()
    assert sleeps == []

    # 2nd acquire: bucket empty → must wait ~1s for a token.
    await bucket.acquire()
    assert len(sleeps) == 1
    assert sleeps[0] == pytest.approx(1.0)

    # 3rd acquire happens right after the sleep returns (clock advanced by 1s).
    # With the bug, last_refill stayed at the 2nd acquire's start, so elapsed
    # would span the slept second and refill a free token → NO sleep here.
    # With the fix, last_refill == now, elapsed == 0 → must wait ~1s again.
    await bucket.acquire()
    assert len(sleeps) == 2, "3rd acquire got a free token — slept interval double-counted"
    assert sleeps[1] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_token_bucket_burst_then_steady(monkeypatch):
    clock = _Clock()
    sleeps: list[float] = []

    async def fake_sleep(d):
        sleeps.append(d)
        clock.t += d

    monkeypatch.setattr(rl.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(rl.asyncio, "sleep", fake_sleep)

    bucket = rl.TokenBucket(rate=2.0, burst=3)  # 2 tokens/sec, burst 3

    # Burst: 3 immediate acquires.
    for _ in range(3):
        await bucket.acquire()
    assert sleeps == []

    # 4th: empty → wait 1/rate = 0.5s.
    await bucket.acquire()
    assert sleeps == [pytest.approx(0.5)]

    # 5th right after: with the fix, must wait another 0.5s (steady-state).
    await bucket.acquire()
    assert len(sleeps) == 2
    assert sleeps[1] == pytest.approx(0.5)

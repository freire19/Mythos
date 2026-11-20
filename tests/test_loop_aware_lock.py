"""Loop-aware lock — cross-loop contention must not crash.

DEEP_RESILIENCE #D045/#D048: a plain asyncio.Lock stored at module scope /
in a singleton __init__ binds to the first loop that CONTENDS it (CPython
3.12 only calls Lock._get_loop() when a waiter exists), then raises
`RuntimeError: <Lock> is bound to a different event loop` on the next turn's
loop. The CLI runs asyncio.run() per turn, so a 2nd turn with delegate_parallel
(real contention) would crash. LoopAwareLock rebinds lazily per loop.

Each `asyncio.run()` below creates a FRESH event loop — the exact trigger.
The `gather` of 3 coroutines forces contention (waiters), which is what binds
the lock. With the fix, the 2nd run must NOT raise.
"""
from __future__ import annotations

import asyncio

import httpx

from alpha._http_singleton import LoopAwareClient
from alpha._loop_lock import LoopAwareLock


def test_loop_aware_lock_survives_cross_loop_contention():
    lock = LoopAwareLock()

    async def hold():
        async with lock():
            await asyncio.sleep(0.01)  # hold long enough to create waiters

    async def contend():
        await asyncio.gather(hold(), hold(), hold())

    asyncio.run(contend())  # binds to loop A under contention
    asyncio.run(contend())  # loop B — must not raise "bound to a different loop"


def test_loop_aware_lock_rebinds_lock_object_per_loop():
    lock = LoopAwareLock()
    seen = []

    async def grab():
        async with lock():
            seen.append(id(lock()))

    asyncio.run(grab())
    first = seen[-1]
    asyncio.run(grab())
    second = seen[-1]
    # A new loop → a freshly created underlying asyncio.Lock.
    assert first != second


def test_token_bucket_survives_cross_loop_contention():
    # #D045 repro: the bucket lock is contended on every delegate_parallel call.
    from alpha._rate_limiter import TokenBucket

    # burst=2 forces the 3rd acquirer to wait → guaranteed waiter (binds lock);
    # high rate keeps the wait ~1ms so the test is fast.
    bucket = TokenBucket(rate=1000.0, burst=2)

    async def contend():
        await asyncio.gather(*(bucket.acquire() for _ in range(4)))

    asyncio.run(contend())
    asyncio.run(contend())  # must not raise


def test_loop_aware_client_survives_cross_loop_contention():
    # #D048 repro: two sub-agents rebuilding the shared HTTP client in a new loop.
    client = LoopAwareClient("test", lambda: httpx.AsyncClient())

    async def contend():
        await asyncio.gather(client.get(), client.get(), client.get())

    try:
        asyncio.run(contend())   # slow path under contention → binds lock
        asyncio.run(contend())   # new loop → slow path again → must not raise
    finally:
        asyncio.run(client.close())


def test_loop_aware_client_returns_same_client_within_a_loop():
    client = LoopAwareClient("test2", lambda: httpx.AsyncClient())

    async def go():
        a = await client.get()
        b = await client.get()
        assert a is b  # cached within the same loop
        await client.close()

    asyncio.run(go())

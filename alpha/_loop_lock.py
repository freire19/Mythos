"""Loop-aware asyncio.Lock for module-level / singleton state.

The CLI runs `asyncio.run()` once per REPL turn (`main.py`), so a fresh event
loop is created each turn. A plain `asyncio.Lock()` stored at module scope or
in a singleton's `__init__` binds to the FIRST loop that *contends* it —
CPython 3.12 only calls `Lock._get_loop()` when a waiter appears — and then
raises `RuntimeError: <Lock> is bound to a different event loop` on the next
turn's contention. The bug is intermittent (only fires under real contention,
e.g. `delegate_parallel`), which is why it survived several audits.

`LoopAwareLock` rebinds the underlying lock lazily whenever the running loop
changes, collapsing the pattern that `database_tools._get_pg_pools_lock` and
`BrowserSession._get_lock` each re-implemented (DEEP_RESILIENCE #D044/#D045/
#D048/#D049).

Usage:

    _lock = LoopAwareLock()
    ...
    async with _lock():   # resolves to the current loop's asyncio.Lock
        ...

`__call__` returns the loop's `asyncio.Lock`, so `async with _lock():`
acquires and releases that exact lock object (the loop never changes within a
single `async with`).
"""

from __future__ import annotations

import asyncio


class LoopAwareLock:
    """An `asyncio.Lock` that re-creates itself when the running loop changes."""

    __slots__ = ("_lock", "_loop")

    def __init__(self) -> None:
        self._lock: asyncio.Lock | None = None
        self._loop: object | None = None

    def __call__(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._lock is None or self._loop is not loop:
            self._lock = asyncio.Lock()
            self._loop = loop
        return self._lock

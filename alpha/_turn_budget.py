"""Turn-level wall-clock budget — shared between the agent loop and the
executor so a single long-running tool can't blow past `MAX_TURN_SECONDS`
silently.

The agent loop wraps its iteration loop in `use_deadline(turn_start + budget)`,
a context manager that sets the ContextVar and resets it on exit via the
Token-based lifecycle. The executor calls `remaining()` before each
`asyncio.wait_for(tool_call)` and clamps `tool_timeout` down to the
remaining budget. A tool that asks for 300s inside a turn with 40s left
now runs at most 40s and the agent loop gets control back in time to
emit a clean `max_turn_seconds` anomaly.

ContextVar isolation guarantees:
- `delegate_parallel` wraps each sub-agent coroutine in `asyncio.gather`,
  which creates Tasks. Each Task gets its own copy of the context (PEP 567)
  so sub-agent `use_deadline()` doesn't leak back to the parent.
- `delegate_task` (sequential) awaits the sub-agent directly in the parent's
  Task. Without `use_deadline`'s try/finally reset, the sub-agent's
  `set_deadline` permanently overwrites the parent's deadline — CODE_REVIEW2
  finding #1. The context manager closes that leak.
- The "capped" form (`use_deadline(min_with_inherited=True)`) ensures a
  sub-agent's budget never extends beyond the parent's remaining time —
  CODE_REVIEW2 finding #4. Parent with 30s left spawning a sub-agent with
  120s budget gets capped to 30s.
"""

from __future__ import annotations

import math
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Optional

_turn_deadline: ContextVar[Optional[float]] = ContextVar(
    "_turn_deadline", default=None
)


def set_deadline(deadline_monotonic: Optional[float]) -> object:
    """Pin the deadline for the current task (and its callees).

    `deadline_monotonic` is a `time.monotonic()` reading, not a relative
    timeout. Returns the ContextVar Token — callers that re-enter
    `run_agent` in the same asyncio task MUST reset via `reset(token)`
    or use `use_deadline()` context manager (preferred).
    """
    return _turn_deadline.set(deadline_monotonic)


def reset(token: object) -> None:
    """Restore the previous deadline. Pairs with `set_deadline` Token."""
    _turn_deadline.reset(token)


def set_deadline_capped(deadline_monotonic: Optional[float]) -> object:
    """Pin a deadline that is automatically capped to the inherited one.

    Effective deadline = ``min(deadline_monotonic, inherited)`` so a
    sub-agent's budget can never extend past the parent's remaining
    time. When no parent deadline exists, behaves identically to
    `set_deadline`. Returns the ContextVar Token for the caller to
    pair with `reset()`.

    Added in CODE_REVIEW4 #5 to eliminate the `_turn_deadline` private
    import from sibling modules (encapsulation violation).

    CODE_REVIEW5 #2: passing ``deadline_monotonic=None`` CLEARS the
    deadline (sets ContextVar to None → ``remaining()`` returns inf).
    This is NOT a no-op. Callers that meant "keep current deadline"
    should not call this function at all; callers that meant "remove
    clamp" should be explicit by using ``reset(token)`` after a
    matching ``set_deadline()`` call.
    """
    inherited = _turn_deadline.get()
    if inherited is not None and deadline_monotonic is not None:
        effective = min(deadline_monotonic, inherited)
    else:
        effective = deadline_monotonic
    return _turn_deadline.set(effective)


@contextmanager
def use_deadline(
    deadline_monotonic: Optional[float],
    *,
    cap_to_inherited: bool = True,
) -> Iterator[None]:
    """Scope a deadline for the duration of a block, then restore.

    With `cap_to_inherited=True` (default), the effective deadline is
    `min(deadline_monotonic, current_inherited_deadline)` — a sub-agent
    can never extend past the parent's deadline. Pass False only when
    you genuinely want a fresh budget unrelated to the surrounding scope
    (e.g. top-level REPL invocation where there IS no parent).
    """
    inherited = _turn_deadline.get()
    if cap_to_inherited and inherited is not None and deadline_monotonic is not None:
        effective = min(deadline_monotonic, inherited)
    else:
        effective = deadline_monotonic
    token = _turn_deadline.set(effective)
    try:
        yield
    finally:
        _turn_deadline.reset(token)


# Seconds this task spent blocked on the USER (approval prompts), summed
# over the turn. The budget measures how long the agent has been working;
# a prompt left unanswered overnight must not kill the turn the moment the
# user finally approves.
_user_wait: ContextVar[float] = ContextVar("_user_wait", default=0.0)


def note_user_wait(seconds: float) -> None:
    """Credit `seconds` spent waiting for the user back to the turn: the
    deadline moves forward by the same amount and `user_wait_total()`
    grows so the agent loop can discount it from the elapsed time."""
    if seconds <= 0:
        return
    _user_wait.set(_user_wait.get() + seconds)
    d = _turn_deadline.get()
    if d is not None:
        _turn_deadline.set(d + seconds)


def user_wait_total() -> float:
    """Total seconds credited by `note_user_wait` in this task."""
    return _user_wait.get()


def remaining() -> float:
    """Seconds left in the current turn's wall-clock budget, or +inf
    when no deadline is set (callers should treat that as 'no clamp')."""
    d = _turn_deadline.get()
    if d is None:
        return math.inf
    return max(0.0, d - time.monotonic())


def clamp(timeout_seconds: float) -> float:
    """Return min(timeout_seconds, remaining()). Useful in `wait_for`."""
    return min(timeout_seconds, remaining())

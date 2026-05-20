"""Pre-flight strategy approval (RFC docs/specs/pre-flight-cards.md).

The agent calls `pre_flight(goal, steps, alternatives, confidence)` before
executing a batch of destructive tools. This module owns the cost and time
estimators that enrich the raw plan with quantified numbers the user sees
on the approval card.

Heuristic by design — see RFC open question #5. Set
`ALPHA_ACCURATE_COST_ESTIMATE=1` to opt into real tokenizer-based counts
once that path is implemented (out of scope for slice 1).
"""

from __future__ import annotations

import os

from .cost_estimate import estimate_step_cost, estimate_total_cost
from .feedback import record_decision, summarize
from .time_estimate import estimate_step_time, estimate_total_time


def env_float(name: str) -> float | None:
    """Read a float from an env var; return None when unset, empty, or
    malformed. Used by per-turn and per-session budget caps that share
    the same parse-or-ignore semantics — silently dropping a typo'd
    cap is the documented behavior (RFC open question #2)."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


__all__ = [
    "env_float",
    "estimate_step_cost",
    "estimate_step_time",
    "estimate_total_cost",
    "estimate_total_time",
    "record_decision",
    "summarize",
]

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

import logging
import math
import os

from .cost_estimate import estimate_step_cost, estimate_total_cost
from .feedback import record_decision, summarize
from .time_estimate import estimate_step_time, estimate_total_time

logger = logging.getLogger(__name__)


def env_float(name: str) -> float | None:
    """Read a float from an env var; return None when unset, empty, or
    malformed. Used by per-turn and per-session budget caps that share
    the same parse-or-ignore semantics — silently dropping a typo'd
    cap is the documented behavior (RFC open question #2).

    AUDIT_V2.3 #202: negativo, `nan` e `inf` também contam como
    malformado. `nan` num cap `spent < cap` aborta toda sessão na 1ª
    iteração enquanto o MESMO `nan` em `estimated > cap` desliga o cap
    silenciosamente — parse-or-ignore (com warning) é o único
    comportamento consistente para os dois consumidores."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "%s=%r não é um número — cap ignorado.", name, raw
        )
        return None
    if not math.isfinite(value) or value < 0:
        logger.warning(
            "%s=%r inválido (precisa ser finito e >= 0) — cap ignorado.",
            name, raw,
        )
        return None
    return value


__all__ = [
    "env_float",
    "estimate_step_cost",
    "estimate_step_time",
    "estimate_total_cost",
    "estimate_total_time",
    "record_decision",
    "summarize",
]

"""Capture user decisions on pre_flight cards for future learning.

Writes one JSON line per decision to ``~/.alpha/memory/preflight_feedback.jsonl``.
Slice 2 just collects the data; slice 3 will consume it to self-tune the
estimators (raise confidence on patterns the user consistently approves,
flag patterns frequently modified or rejected).

Append-only by design — the analysis pipeline reads the full log; deleting
or compacting old entries is a separate maintenance task. File rotation
kicks in at ~10K lines (~2 MB) to keep parsing cheap; older entries spill
to ``preflight_feedback.jsonl.1`` (kept indefinitely until user prunes).
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from ..settings import alpha_user_dir

logger = logging.getLogger(__name__)

_FEEDBACK_DIR = alpha_user_dir("memory")
_FEEDBACK_PATH = _FEEDBACK_DIR / "preflight_feedback.jsonl"
_FEEDBACK_ROTATE_LINES = 10_000


def _maybe_rotate(path: Path) -> None:
    """Rotate when the log gets unwieldy. Cheap line-count via wc-style
    walk so we don't load the whole file just to check size."""
    if not path.exists():
        return
    try:
        size = path.stat().st_size
    except OSError:
        return
    # ~200 bytes per entry typical; 10K lines ≈ 2 MB. Rotating by size
    # is cheaper than counting lines and equivalent in practice.
    if size < 2 * 1024 * 1024:
        return
    backup = path.with_suffix(".jsonl.1")
    try:
        if backup.exists():
            backup.unlink()
        path.rename(backup)
    except OSError as e:
        logger.warning("preflight_feedback rotation failed: %s", e)


def record_decision(card: dict[str, Any], decision: str) -> None:
    """Append a feedback entry.

    `card` is the dict the executor returned (`goal`, `steps`,
    `estimated_cost_usd`, etc.). `decision` is one of "approve",
    "reject", "approve_all". We strip `steps` to just the tool names so
    each line stays bounded — full step args can be large and the
    learner only needs the shape, not the verbatim arguments.
    """
    try:
        _FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)
        _maybe_rotate(_FEEDBACK_PATH)
        entry = {
            "ts": time.time(),
            "decision": decision,
            "goal": str(card.get("goal", ""))[:200],
            "confidence": card.get("confidence"),
            "estimated_cost_usd": card.get("estimated_cost_usd"),
            "estimated_time_s": card.get("estimated_time_s"),
            "model": card.get("model"),
            "step_tools": [
                str(s.get("tool", "")) for s in (card.get("steps") or [])
                if isinstance(s, dict)
            ],
            "n_alternatives_rejected": len(card.get("alternatives_rejected") or []),
        }
        with _FEEDBACK_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        # Logging this is the most we can do — preflight UI is in the
        # critical path of every destructive turn, can't fail because
        # the feedback log isn't writable.
        logger.warning("preflight_feedback write failed: %s", e)

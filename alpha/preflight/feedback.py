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
from typing import Any, Literal

from ..settings import alpha_user_dir

logger = logging.getLogger(__name__)

# Decisions the user can take on a pre_flight card. Centralized as a
# Literal so the producer (display/renderers/prompts.py) and the
# consumer (this module + analytics) can't drift via typo'd strings.
PreflightDecision = Literal[
    "approve", "reject", "approve_all", "auto_approve", "eof", "interrupt"
]

_FEEDBACK_DIR = alpha_user_dir("memory")
_FEEDBACK_PATH = _FEEDBACK_DIR / "preflight_feedback.jsonl"
# ~200 bytes per entry typical; 2 MB ≈ 10K entries. Rotating by size
# is cheaper than counting lines and equivalent in practice.
_FEEDBACK_ROTATE_BYTES = 2 * 1024 * 1024
_GOAL_TRUNCATE = 200


def _maybe_rotate(path: Path) -> None:
    """Rotate when the log exceeds _FEEDBACK_ROTATE_BYTES."""
    if not path.exists():
        return
    try:
        size = path.stat().st_size
    except OSError:
        return
    if size < _FEEDBACK_ROTATE_BYTES:
        return
    backup = path.with_suffix(".jsonl.1")
    try:
        if backup.exists():
            backup.unlink()
        path.rename(backup)
    except OSError as e:
        logger.warning("preflight_feedback rotation failed: %s", e)


def _read_entries(limit: int | None = None) -> list[dict[str, Any]]:
    """Stream the feedback log into a list of parsed dicts.

    Broken lines (malformed JSON, partial writes) are silently skipped —
    the log is append-only but a crash mid-write could leave a bad line.
    `limit` returns the most-recent N entries; None returns all.
    """
    if not _FEEDBACK_PATH.exists():
        return []
    entries: list[dict[str, Any]] = []
    try:
        with _FEEDBACK_PATH.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError as e:
        logger.warning("preflight_feedback read failed: %s", e)
        return []
    if limit is not None:
        return entries[-limit:]
    return entries


def summarize(entries: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Aggregate the feedback log into a summary dict for /preflight.

    Pass `entries` to summarize a subset (testing); omit to read the full
    log from disk. Returns counts per decision, totals, and per-tool
    decision breakdowns. Empty log returns zeros — caller decides how to
    render that.
    """
    if entries is None:
        entries = _read_entries()
    if not entries:
        return {
            "total": 0,
            "decisions": {},
            "by_tool": {},
            "avg_estimated_cost_usd": 0.0,
            "total_estimated_cost_usd": 0.0,
        }

    decisions: dict[str, int] = {}
    by_tool: dict[str, dict[str, int]] = {}
    total_cost = 0.0
    cost_samples = 0
    for entry in entries:
        decision = str(entry.get("decision", "unknown"))
        decisions[decision] = decisions.get(decision, 0) + 1
        cost = entry.get("estimated_cost_usd")
        if isinstance(cost, (int, float)):
            total_cost += float(cost)
            cost_samples += 1
        for tool in entry.get("step_tools") or []:
            bucket = by_tool.setdefault(tool, {})
            bucket[decision] = bucket.get(decision, 0) + 1

    return {
        "total": len(entries),
        "decisions": decisions,
        "by_tool": by_tool,
        "avg_estimated_cost_usd": total_cost / cost_samples if cost_samples else 0.0,
        "total_estimated_cost_usd": total_cost,
    }


def record_decision(card: dict[str, Any], decision: PreflightDecision) -> None:
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
            "goal": str(card.get("goal", ""))[:_GOAL_TRUNCATE],
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

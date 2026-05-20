"""Heuristic time estimator for a planned tool call.

Hard-coded per-tool-family medians — good enough to surface "this batch
will take ~30s vs ~5min" on the approval card.

A future slice will read per-tool medians from this user's actual latency
history; today `alpha/stats.py` keeps that data in memory only, so the
on-disk source doesn't exist yet. When it does, replace the defaults
below with a `~/.alpha/stats/tool_latency.jsonl` median lookup that
falls back to these defaults for cold-start sessions.
"""

from __future__ import annotations

# Seconds — wall-clock median observed in `/stats` output during dev.
# Conservative: assume the slower side of typical so the user isn't
# annoyed by under-estimates.
_DEFAULT_STEP_TIME_S = {
    "read_file": 0.1,
    "write_file": 0.2,
    "edit_file": 0.3,
    "execute_shell": 2.0,       # depends entirely on the command
    "execute_python": 5.0,      # cold-import overhead
    "execute_pipeline": 3.0,
    "grep_files": 0.4,
    "list_files": 0.2,
    "search_files": 0.5,
    "git_status": 0.3,
    "git_diff": 0.4,
    "git_log": 0.3,
    "git_commit": 1.0,
    "delegate_task": 30.0,      # sub-agent loops are the long ones
    "delegate_parallel": 45.0,  # multiple sub-agents, gated by slowest
    "delegate_consensus": 40.0,
}

_DEFAULT_STEP_TIME_FALLBACK_S = 1.0
# LLM round-trip after each tool completes — varies wildly by provider
# but ~3s is a reasonable midpoint for the agent's "process result and
# decide next step" turn.
_LLM_TURN_OVERHEAD_S = 3.0


def estimate_step_time(tool: str) -> float:
    """Seconds estimate for one tool execution. Excludes LLM round-trip."""
    return _DEFAULT_STEP_TIME_S.get(tool, _DEFAULT_STEP_TIME_FALLBACK_S)


def estimate_total_time(steps: list[dict]) -> float:
    """Sum of per-step estimates plus one LLM round-trip per step.

    Each tool call triggers a follow-up LLM turn (the agent reads the
    result and decides next move). For a 5-step plan that's 5 round-trips
    on top of the tool wall-clock — ignoring it would under-estimate by
    a factor of 2-3x for short tool calls.
    """
    tool_time = sum(estimate_step_time(str(s.get("tool", ""))) for s in steps)
    overhead = _LLM_TURN_OVERHEAD_S * len(steps)
    return tool_time + overhead

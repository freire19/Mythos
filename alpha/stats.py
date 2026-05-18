"""
Session-level usage stats (Plano-Upgrade-v3 H1 #4 phase 2 — `/stats`).

Tracks lightweight counters the user can inspect mid-session: total
iterations, per-tool call counts and cumulative latency, wall-clock
session age. Cost tracking lives in `alpha.cost`; this module focuses
on volumetric / temporal stats.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class _ToolMetrics:
    calls: int = 0
    total_ms: float = 0.0
    last_ms: float = 0.0


@dataclass
class _Session:
    started_at: float = field(default_factory=time.monotonic)
    iterations: int = 0
    tools: dict[str, _ToolMetrics] = field(default_factory=dict)
    approvals_required: int = 0
    approvals_granted: int = 0

    def reset(self) -> None:
        self.started_at = time.monotonic()
        self.iterations = 0
        self.tools.clear()
        self.approvals_required = 0
        self.approvals_granted = 0


_session = _Session()


def record_iteration() -> None:
    _session.iterations += 1


def record_tool(name: str, latency_ms: float) -> None:
    m = _session.tools.get(name)
    if m is None:
        m = _ToolMetrics()
        _session.tools[name] = m
    m.calls += 1
    m.total_ms += float(latency_ms)
    m.last_ms = float(latency_ms)


def record_approval(granted: bool) -> None:
    _session.approvals_required += 1
    if granted:
        _session.approvals_granted += 1


def reset_session() -> None:
    _session.reset()


def session_age_seconds() -> float:
    return time.monotonic() - _session.started_at


def session_summary() -> dict:
    tools = [
        {
            "name": name,
            "calls": m.calls,
            "total_ms": m.total_ms,
            "avg_ms": m.total_ms / m.calls if m.calls else 0.0,
        }
        for name, m in _session.tools.items()
    ]
    tools.sort(key=lambda t: t["calls"], reverse=True)
    return {
        "uptime_s": session_age_seconds(),
        "iterations": _session.iterations,
        "tool_calls_total": sum(m.calls for m in _session.tools.values()),
        "approvals_required": _session.approvals_required,
        "approvals_granted": _session.approvals_granted,
        "tools": tools,
    }

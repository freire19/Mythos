"""Resumable sub-agent sessions (parity wave 2026-11, item 6).

A `delegate_task` sub-agent used to be fire-and-forget: once it returned,
its conversation was gone and the only way to follow up was a brand-new
cold sub-agent. This registry keeps the finished in-process sub-agent's
prepared run (`prep`: messages, filtered tools, gated get_tool, approval,
workspace, provider, temperature, budgets, scratch dir) so the parent can
send it another message with `agent_message(agent_id, message)` and it
continues with its full history — the same thing Claude Code does with
SendMessage to a previously spawned agent.

Scope and limits:
- in-process sub-agents only (background children are separate processes);
- session-global, bounded to `MAX_SESSIONS` most recent (oldest evicted);
- a resumed run gets a fresh iteration/time budget but keeps the SAME tool
  filters, blocklist and workspace it was created with — resuming never
  widens what the sub-agent may do;
- `/clear` forgets every session.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from types import SimpleNamespace

MAX_SESSIONS = 8


@dataclass
class SubagentSession:
    agent_id: str
    prep: SimpleNamespace
    label: str = ""
    task_preview: str = ""
    turns: int = 0
    last_status: str = ""
    last_reply_preview: str = ""
    updated_at: float = field(default_factory=time.time)
    busy: bool = False

    def as_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "label": self.label,
            "task": self.task_preview,
            "turns": self.turns,
            "last_status": self.last_status,
            "last_reply": self.last_reply_preview,
            "messages": len(getattr(self.prep, "messages", []) or []),
            "busy": self.busy,
            "age_s": round(time.time() - self.updated_at, 1),
        }


_sessions: dict[str, SubagentSession] = {}
_lock = threading.Lock()


def _preview(text: str, n: int = 120) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def remember(prep: SimpleNamespace, *, task: str, status: str, reply: str, label: str = "") -> None:
    """Store/refresh the session for `prep.agent_id` after a completed run."""
    with _lock:
        s = _sessions.get(prep.agent_id)
        if s is None:
            s = SubagentSession(agent_id=prep.agent_id, prep=prep, label=label,
                                task_preview=_preview(task))
            _sessions[prep.agent_id] = s
        s.prep = prep
        s.turns += 1
        s.last_status = status
        s.last_reply_preview = _preview(reply, 200)
        s.updated_at = time.time()
        s.busy = False
        if len(_sessions) > MAX_SESSIONS:
            oldest = sorted(_sessions.values(), key=lambda x: x.updated_at)
            for victim in oldest[: len(_sessions) - MAX_SESSIONS]:
                if not victim.busy:
                    _sessions.pop(victim.agent_id, None)


def get(agent_id: str) -> SubagentSession | None:
    with _lock:
        return _sessions.get(agent_id)


def acquire(agent_id: str) -> SubagentSession | None:
    """Mark the session busy for a resume; None if unknown or already busy."""
    with _lock:
        s = _sessions.get(agent_id)
        if s is None or s.busy:
            return None
        s.busy = True
        return s


def release(agent_id: str) -> None:
    with _lock:
        s = _sessions.get(agent_id)
        if s is not None:
            s.busy = False


def list_sessions() -> list[dict]:
    with _lock:
        return [s.as_dict() for s in sorted(_sessions.values(), key=lambda x: -x.updated_at)]


def reset() -> None:
    """Forget every session (`/clear`, tests)."""
    with _lock:
        _sessions.clear()

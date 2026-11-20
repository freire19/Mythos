"""In-REPL scheduling — `/loop` (parity wave 2026-11, item 7).

`/loop 5m <prompt>` re-runs `<prompt>` (free text or a slash command)
every 5 minutes while the REPL is open; `/loop once 10m <prompt>` fires it
a single time. Schedules live in this process only (no daemon, no cron):
when the REPL exits they are gone, which is the right scope for "poll CI
every 5 minutes" or "remind me in 20 minutes to check the deploy".

Firing model: the REPL consumes due prompts at the input boundary. While
the user is typing nothing, `repl_input.read_input` arms a timer that
exits the prompt_toolkit prompt when the next schedule is due and feeds
the prompt in as if the user had typed it (echoed with a ⏰ marker). A
prompt never fires mid-turn, and never while the input buffer holds text
the user is composing — it waits for the next idle moment.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field

MAX_SCHEDULES = 10
MIN_INTERVAL_S = 10.0
MAX_INTERVAL_S = 24 * 3600.0

_INTERVAL_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(s|sec|m|min|h|hr)?$", re.IGNORECASE)
_UNIT_S = {"s": 1, "sec": 1, "m": 60, "min": 60, "h": 3600, "hr": 3600}


def parse_interval(text: str) -> float:
    """'30s' / '5m' / '1.5h' / '90' (seconds) → seconds. Raises ValueError."""
    m = _INTERVAL_RE.match((text or "").strip())
    if not m:
        raise ValueError(f"intervalo inválido: {text!r} (use ex. 30s, 5m, 1h)")
    value = float(m.group(1)) * _UNIT_S[(m.group(2) or "s").lower()]
    if value < MIN_INTERVAL_S:
        raise ValueError(f"intervalo mínimo é {int(MIN_INTERVAL_S)}s")
    if value > MAX_INTERVAL_S:
        raise ValueError("intervalo máximo é 24h")
    return value


def format_interval(seconds: float) -> str:
    if seconds % 3600 == 0:
        return f"{int(seconds // 3600)}h"
    if seconds % 60 == 0:
        return f"{int(seconds // 60)}m"
    return f"{int(seconds)}s"


@dataclass
class Schedule:
    id: int
    interval_s: float
    prompt: str
    max_fires: int | None = None
    fires: int = 0
    next_at: float = field(default_factory=time.monotonic)
    created_at: float = field(default_factory=time.time)

    @property
    def remaining(self) -> float:
        return max(0.0, self.next_at - time.monotonic())

    @property
    def exhausted(self) -> bool:
        return self.max_fires is not None and self.fires >= self.max_fires

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "interval": format_interval(self.interval_s),
            "interval_s": self.interval_s,
            "prompt": self.prompt,
            "fires": self.fires,
            "max_fires": self.max_fires,
            "next_in_s": round(self.remaining, 1),
        }


_schedules: dict[int, Schedule] = {}
_next_id = 1
_lock = threading.Lock()


def add(interval_s: float, prompt: str, *, max_fires: int | None = None) -> Schedule:
    global _next_id
    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("prompt vazio")
    with _lock:
        if len(_schedules) >= MAX_SCHEDULES:
            raise ValueError(f"máximo de {MAX_SCHEDULES} loops ativos")
        s = Schedule(
            id=_next_id, interval_s=float(interval_s), prompt=prompt,
            max_fires=max_fires, next_at=time.monotonic() + float(interval_s),
        )
        _schedules[s.id] = s
        _next_id += 1
        return s


def remove(schedule_id: int) -> Schedule | None:
    with _lock:
        return _schedules.pop(int(schedule_id), None)


def clear() -> int:
    with _lock:
        n = len(_schedules)
        _schedules.clear()
        return n


def list_schedules() -> list[dict]:
    with _lock:
        return [s.as_dict() for s in sorted(_schedules.values(), key=lambda x: x.id)]


def active() -> bool:
    with _lock:
        return bool(_schedules)


def next_due_in() -> float | None:
    """Seconds until the earliest schedule fires, or None when none exist."""
    with _lock:
        if not _schedules:
            return None
        return min(s.remaining for s in _schedules.values())


def pop_due(now: float | None = None) -> list[tuple[int, str]]:
    """Return `(id, prompt)` for every schedule that is due, advancing each
    one's `next_at` (and dropping exhausted one-shots). Order: by id."""
    now = time.monotonic() if now is None else now
    out: list[tuple[int, str]] = []
    with _lock:
        for s in sorted(_schedules.values(), key=lambda x: x.id):
            if s.next_at <= now:
                s.fires += 1
                out.append((s.id, s.prompt))
                if s.exhausted:
                    _schedules.pop(s.id, None)
                else:
                    # Re-anchor on `now`, not on the stale next_at: a REPL that
                    # was busy for 3 intervals should fire once, not 3 times.
                    s.next_at = now + s.interval_s
    return out


def pop_one_due(now: float | None = None) -> tuple[int, str] | None:
    """Like `pop_due` but advances and returns only the earliest-id due
    schedule, leaving the others due for the next call. Used by the REPL,
    which delivers one prompt per input read."""
    now = time.monotonic() if now is None else now
    with _lock:
        for s in sorted(_schedules.values(), key=lambda x: x.id):
            if s.next_at <= now:
                s.fires += 1
                if s.exhausted:
                    _schedules.pop(s.id, None)
                else:
                    s.next_at = now + s.interval_s
                return (s.id, s.prompt)
    return None


def reset_for_tests() -> None:
    global _next_id
    with _lock:
        _schedules.clear()
        _next_id = 1

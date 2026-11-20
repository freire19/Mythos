"""Desktop notifications for the REPL (parity wave 2026-11, follow-up).

When the user walks away during a long turn, Alpha tells the desktop when
the turn finishes or when it is blocked on an approval prompt — mirroring
Claude Code's idle/permission notifications.

- `ALPHA_NOTIFY=auto|on|off` (`config.notify_mode`). Auto: on when stdout is
  a TTY and a backend exists; headless (`-p`), sub-agents and CI never
  notify.
- `ALPHA_NOTIFY_AFTER` (seconds, default 20): only turns at least this long
  trigger a notification — a 3-second answer the user is still looking at
  does not need one.

Backends, in order: `notify-send` (Linux, libnotify), `osascript` (macOS),
else a terminal bell (`\\a`) which most terminals surface as an urgency
hint / dock bounce. The command runs in a daemon thread with a short
timeout and never raises into the REPL. Text is clipped and control chars
stripped before it reaches the shell-free `subprocess` call.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
import time

logger = logging.getLogger(__name__)

TITLE = "Alpha Code"
_BODY_CAP = 160
_CMD_TIMEOUT_S = 3.0

_turn_started: float | None = None
_lock = threading.Lock()


# ── enablement ───────────────────────────────────────────────────────


def _backend() -> str | None:
    if sys.platform.startswith("linux") and shutil.which("notify-send"):
        return "notify-send"
    if sys.platform == "darwin" and shutil.which("osascript"):
        return "osascript"
    return "bell"


def enabled() -> bool:
    from .config import notify_mode

    mode = notify_mode()
    if mode == "off":
        return False
    if os.environ.get("ALPHA_DELEGATED") == "1" or os.environ.get("ALPHA_SUBAGENT_HEADLESS") == "1":
        return False
    if mode == "on":
        return True
    # auto
    try:
        return sys.stdout.isatty() and not os.environ.get("CI")
    except (AttributeError, ValueError):
        return False


def threshold_s() -> float:
    from .config import notify_after_seconds

    return float(notify_after_seconds())


# ── turn tracking ────────────────────────────────────────────────────


def turn_started() -> None:
    global _turn_started
    with _lock:
        _turn_started = time.monotonic()


def turn_elapsed() -> float:
    with _lock:
        return 0.0 if _turn_started is None else time.monotonic() - _turn_started


def _long_enough() -> bool:
    return turn_elapsed() >= threshold_s()


def turn_finished(reply: str = "") -> bool:
    """Call when the REPL turn ends. Notifies only for long turns."""
    global _turn_started
    try:
        if not enabled() or not _long_enough():
            return False
        secs = int(turn_elapsed())
        body = _clean(reply) or "Turno concluído."
        return send(f"Pronto após {secs}s", body)
    finally:
        with _lock:
            _turn_started = None


def approval_needed(tool_name: str) -> bool:
    """Call right before an approval prompt blocks."""
    if not enabled() or not _long_enough():
        return False
    return send("Aguardando aprovação", f"{_clean(tool_name)} precisa da sua confirmação.")


def background_finished(detail: str) -> bool:
    """A background task/sub-agent finished — always worth a nudge when on."""
    if not enabled():
        return False
    return send("Tarefa em background terminou", _clean(detail))


# ── delivery ─────────────────────────────────────────────────────────


def _clean(text: str) -> str:
    from ._text_safety import strip_control_and_bidi

    t = strip_control_and_bidi(str(text or "")).strip()
    t = " ".join(t.split())
    return t[:_BODY_CAP]


def send(title: str, body: str) -> bool:
    """Fire-and-forget. Returns whether a delivery was attempted."""
    backend = _backend()
    title = _clean(title) or TITLE
    body = _clean(body)
    if backend == "bell":
        try:
            sys.stdout.write("\a")
            sys.stdout.flush()
            return True
        except (OSError, ValueError):
            return False
    if backend == "notify-send":
        argv = ["notify-send", "--app-name", TITLE, "--urgency", "normal", title, body]
    else:  # osascript
        script = (
            f'display notification "{_osa(body)}" with title "{_osa(TITLE)}" '
            f'subtitle "{_osa(title)}"'
        )
        argv = ["osascript", "-e", script]
    threading.Thread(target=_run, args=(argv,), daemon=True, name="alpha-notify").start()
    return True


def _osa(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _run(argv: list[str]) -> None:
    try:
        subprocess.run(
            argv, timeout=_CMD_TIMEOUT_S, check=False,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.debug("desktop notification failed: %s", e)


def reset_for_tests() -> None:
    global _turn_started
    with _lock:
        _turn_started = None

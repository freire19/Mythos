"""Arrow-key menu for the approval prompt and `ask_choice`.

`select_menu` draws the options itself and redraws in place as the
highlight moves: ↑/↓ (or k/j) move, Enter confirms, a digit picks that
option directly, and the caller's letter shortcuts (s/a/t/n) keep working.

It needs a real terminal on both ends. `can_select()` says whether that
holds; callers fall back to a typed `input()` answer when it doesn't
(pipes, CI, Windows without termios, tests).
"""

from __future__ import annotations

import os
import sys

from .theme import C, c

_HINT = "↑/↓ para mover · Enter confirma · número escolhe direto"

# Seconds to wait for the rest of an escape sequence after a lone ESC.
_ESC_TIMEOUT = 0.05


def can_select() -> bool:
    """True when the interactive menu can run: POSIX termios available and
    both stdin and stdout attached to a terminal."""
    try:
        import termios  # noqa: F401
        import tty  # noqa: F401
    except ImportError:
        return False
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


class MenuCancelled(Exception):
    """The caller abandoned the menu (its `cancel` event was set)."""


# How often a blocked menu checks whether its caller gave up.
_CANCEL_POLL = 0.2


def _read_key(fd: int, cancel=None) -> str:
    """Block for one keypress and return a symbolic name: 'up', 'down',
    'enter', 'esc', 'interrupt', 'eof', or the literal character.

    With `cancel` (a threading.Event) the wait is polled, so a menu running
    in a worker thread stops reading the terminal when its caller is gone —
    an orphaned reader keeps the tty in raw mode and eats the keystrokes
    meant for the next prompt."""
    import select as _select

    while cancel is not None:
        if cancel.is_set():
            raise MenuCancelled
        ready, _, _ = _select.select([fd], [], [], _CANCEL_POLL)
        if ready:
            break
    data = os.read(fd, 1)
    if not data:
        return "eof"
    ch = data[0]
    if ch == 0x03:
        return "interrupt"
    if ch == 0x04:
        return "eof"
    if ch in (0x0D, 0x0A):
        return "enter"
    if ch == 0x1B:
        seq = b""
        # CSI (`ESC [ A`) and SS3 (`ESC O A`) arrows arrive as a burst.
        while len(seq) < 8:
            ready, _, _ = _select.select([fd], [], [], _ESC_TIMEOUT)
            if not ready:
                break
            nxt = os.read(fd, 1)
            if not nxt:
                break
            seq += nxt
            if len(seq) >= 2 and 0x40 <= nxt[0] <= 0x7E:
                break
        if not seq:
            return "esc"
        if seq[:1] in (b"[", b"O") and seq[-1:] == b"A":
            return "up"
        if seq[:1] in (b"[", b"O") and seq[-1:] == b"B":
            return "down"
        return ""
    if ch >= 0x80:
        # Swallow the rest of a UTF-8 character so it isn't read as keys.
        extra = 3 if ch >= 0xF0 else 2 if ch >= 0xE0 else 1 if ch >= 0xC0 else 0
        buf = data
        for _ in range(extra):
            buf += os.read(fd, 1)
        return buf.decode("utf-8", errors="ignore")
    return chr(ch)


def _menu_lines(options: list[list[str]], current: int) -> list[str]:
    """Rendered rows for the whole menu with `current` (0-based) highlighted."""
    width = len(str(len(options)))
    hang = " " * (4 + width + 2)
    rows: list[str] = []
    for i, opt_lines in enumerate(options):
        active = i == current
        pointer = c(C.VIOLET_GLOW, "❯") if active else " "
        num = c(C.VIOLET_GLOW if active else C.GRAY, f"{i + 1:>{width}}.")
        first = c(C.BOLD, opt_lines[0]) if active else opt_lines[0]
        rows.append(f"  {pointer} {num} {first}")
        for ln in opt_lines[1:]:
            rows.append(f"{hang}{c(C.BOLD, ln) if active else ln}")
    return rows


def select_menu(
    options: list[list[str]],
    shortcuts: dict[str, int] | None = None,
    hint: str = _HINT,
    cancel=None,
) -> int | None:
    """Run the menu and return the chosen 0-based index, or None on Esc.

    `options` holds the already-wrapped display lines of each option.
    `shortcuts` maps a lowercase key to the index it selects immediately.
    Raises KeyboardInterrupt on Ctrl+C and EOFError on Ctrl+D, like `input()`,
    and MenuCancelled when `cancel` (a threading.Event) is set by the caller;
    the terminal mode is restored on every exit path.
    """
    import termios
    import tty

    shortcuts = shortcuts or {}
    n = len(options)
    current = 0
    typed = ""  # digit buffer, only used when there are 10+ options
    out = sys.stdout
    fd = sys.stdin.fileno()

    def draw(first: bool = False) -> None:
        rows = _menu_lines(options, current) + ["", f"  {c(C.GRAY_DARK, hint)}"]
        buf = "" if first else f"\033[{len(rows)}A"
        buf += "".join(f"\r\033[K{row}\n" for row in rows)
        out.write(buf)
        out.flush()

    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        # Read Ctrl+C ourselves: this may run in a worker thread, where a
        # SIGINT would hit the main thread and leave the read blocked.
        new = termios.tcgetattr(fd)
        new[3] &= ~termios.ISIG
        new[6][termios.VMIN] = 1
        new[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSADRAIN, new)
        out.write("\033[?25l")
        draw(first=True)
        while True:
            key = _read_key(fd, cancel)
            if key == "interrupt":
                raise KeyboardInterrupt
            if key == "eof":
                raise EOFError
            if key == "esc":
                return None
            if key == "enter":
                return current
            if key in ("up", "k"):
                current = (current - 1) % n
                typed = ""
            elif key in ("down", "j", "\t"):
                current = (current + 1) % n
                typed = ""
            elif key.isdigit():
                if n <= 9:
                    if 1 <= int(key) <= n:
                        current = int(key) - 1
                        draw()
                        return current
                    continue
                typed = (typed + key)[-len(str(n)):]
                if not 1 <= int(typed) <= n:
                    typed = key
                if 1 <= int(typed) <= n:
                    current = int(typed) - 1
            elif key.lower() in shortcuts:
                current = shortcuts[key.lower()]
                draw()
                return current
            else:
                continue
            draw()
    finally:
        # Drop the hint row and the blank above it; leave the menu on screen.
        try:
            out.write("\033[2A\r\033[J\033[?25h")
            out.flush()
        except Exception:
            pass
        termios.tcsetattr(fd, termios.TCSADRAIN, old)

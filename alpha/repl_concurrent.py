"""Concurrent stdin reader — keep the input row editable while the agent runs.

POSIX-only. On Windows or when stdin/stdout isn't a tty, every entry point
here becomes a no-op and the user keeps the legacy behavior (wait for the
turn to finish before typing).

While active:
- the tty is in raw-ish mode (ICANON/ECHO/ISIG off) so we get char-by-char
  reads and Ctrl+C arrives as `\\x03` instead of SIGINT
- a single daemon thread polls stdin via `select` and edits an in-memory
  buffer; Enter pushes the line to a thread-safe deque
- the input line is rendered on the bottom-most reserved row inside the
  scroll region that `alpha.display.thinking` already carves out

`thinking._draw` checks `is_active()` and skips the status row to avoid
clobbering us with spinner frames.

`pause()` / `resume()` exist to yield stdin briefly to the approval prompt
which still uses cooked-mode `input()`.
"""

from __future__ import annotations

import codecs
import logging
import os
import signal
import sys
import threading
from collections import deque
from contextlib import contextmanager
from typing import Deque, Optional

# Import display colors at top-level (sibling module, can't fail without
# the whole package being broken — better to fail loud at startup than
# silently per-keystroke from inside _render_locked).
from .display.core import C, c

logger = logging.getLogger(__name__)

_DISABLE_ENV = "ALPHA_NO_CONCURRENT_INPUT"
_ENABLE_ENV = "ALPHA_CONCURRENT_INPUT"

_lock = threading.Lock()
_active = False
_thread: Optional[threading.Thread] = None
_stop_flag = threading.Event()
_old_termios: object | None = None
_scroll_region_set = False  # whether start() installed a scroll region

# Buffer / cursor persist across pause()/resume() cycles. Initialized
# once at module load; start() never resets them so mid-typed text
# survives approval prompts.
_buffer = ""
_cursor = 0
_queue: Deque[str] = deque()


def is_active() -> bool:
    return _active


def get_queue() -> Deque[str]:
    return _queue


def drain_queue() -> list[str]:
    """Pop everything from the queue in arrival order."""
    out: list[str] = []
    while _queue:
        out.append(_queue.popleft())
    return out


def _supported() -> bool:
    if os.name != "posix":
        return False
    # OPT-IN (default OFF). This module installs its OWN DECSTBM scroll region
    # (see _install_scroll_region) to pin a concurrent input line at the bottom
    # row. The thinking-spinner ALSO installs a scroll region — two independent
    # DECSTBM managers fighting over the same terminal is what floated the
    # prompt to the top + opened a huge gap + swallowed the recap. The upstream
    # baseline (Alpha_Mythos = v1.22.2) has no concurrent input and renders
    # cleanly, so default to that. Enable type-while-running with
    # ALPHA_CONCURRENT_INPUT=1 (only on terminals where it renders right).
    if os.environ.get(_DISABLE_ENV):
        return False
    if os.environ.get(_ENABLE_ENV) != "1":
        return False
    try:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            return False
    except (ValueError, AttributeError):
        return False
    return True


def _term_size() -> tuple[int, int]:
    """(rows, cols) of the controlling terminal, with a stable fallback."""
    try:
        size = os.get_terminal_size()
        return max(1, size.lines), max(1, size.columns)
    except OSError:
        return 24, 80


def _render_locked() -> None:
    """Repaint the input row. Caller must hold `_lock`.

    Renders a *logical* cursor as a reverse-video character inside the
    visible buffer, then restores the terminal cursor to wherever it was
    before. Critical for correctness: if we left the cursor on the input
    row, the next `print()` from the agent (tokens, tool results) would
    write *into* the user's typed text.
    """
    if not _active:
        return

    rows, cols = _term_size()
    row = rows - 1

    try:
        from .display.core import is_auto_accept
        prompt_color = C.AMBER_SOFT + C.BOLD if is_auto_accept() else C.VIOLET + C.BOLD
    except Exception:
        prompt_color = C.VIOLET + C.BOLD
    prompt_text = "❯ "
    prompt = c(prompt_color, prompt_text)

    # Reserve 1 col padding on the right so the cursor marker doesn't sit at the edge.
    visible_width = max(10, cols - len(prompt_text) - 1)
    if _cursor <= visible_width:
        start = 0
    else:
        start = _cursor - visible_width + 1
    visible = _buffer[start : start + visible_width]
    cursor_pos = _cursor - start  # column within `visible`

    reverse_on = "\033[7m"
    reverse_off = "\033[27m"
    if cursor_pos < len(visible):
        marker_char = visible[cursor_pos]
        body = (
            visible[:cursor_pos]
            + reverse_on + marker_char + reverse_off
            + visible[cursor_pos + 1 :]
        )
    else:
        body = visible + reverse_on + " " + reverse_off

    line = prompt + body
    try:
        sys.stdout.write(f"\033[s\033[{row};1H\033[K{line}\033[u")
        sys.stdout.flush()
    except (OSError, ValueError):
        pass


def _clear_input_row_locked() -> None:
    if not _supported():
        return
    try:
        rows, _ = _term_size()
        sys.stdout.write(f"\033[s\033[{rows - 1};1H\033[K\033[u")
        sys.stdout.flush()
    except (OSError, ValueError):
        pass


def _process_char(ch: str) -> None:
    """Update buffer/cursor in response to one input char (or escape seq).
    Caller holds `_lock`."""
    global _buffer, _cursor

    # Ctrl+C: contextual — clear buffer if anything there; otherwise SIGINT
    # the main thread so asyncio.run() raises KeyboardInterrupt the way it
    # does today.
    if ch == "\x03":
        if _buffer:
            _buffer = ""
            _cursor = 0
            _render_locked()
        else:
            try:
                os.kill(os.getpid(), signal.SIGINT)
            except OSError:
                pass
        return

    # Ctrl+D: queue /exit so the REPL exits after the current turn ends.
    # Only honored on an empty buffer — avoid accidental exits when the
    # user meant "delete forward" mid-line.
    if ch == "\x04":
        if not _buffer:
            _queue.append("/exit")
        return

    if ch in ("\r", "\n"):
        line = _buffer
        _buffer = ""
        _cursor = 0
        if line.strip():
            _queue.append(line)
        _render_locked()
        return

    # Backspace / delete-left
    if ch in ("\x7f", "\x08"):
        if _cursor > 0:
            _buffer = _buffer[: _cursor - 1] + _buffer[_cursor:]
            _cursor -= 1
            _render_locked()
        return

    # Ctrl+U: clear line
    if ch == "\x15":
        _buffer = ""
        _cursor = 0
        _render_locked()
        return

    # Ctrl+A / Ctrl+E: home / end
    if ch == "\x01":
        _cursor = 0
        _render_locked()
        return
    if ch == "\x05":
        _cursor = len(_buffer)
        _render_locked()
        return

    # Printable
    if ch.isprintable():
        _buffer = _buffer[:_cursor] + ch + _buffer[_cursor:]
        _cursor += 1
        _render_locked()
        return

    # Anything else (raw control bytes we don't handle) — ignore.


def _process_escape(seq: str) -> None:
    """Handle ESC sequences for arrow keys / home / end. Caller holds `_lock`."""
    global _cursor, _buffer
    # CSI sequences: ESC [ ...
    if seq == "\x1b[D":  # left
        if _cursor > 0:
            _cursor -= 1
            _render_locked()
    elif seq == "\x1b[C":  # right
        if _cursor < len(_buffer):
            _cursor += 1
            _render_locked()
    elif seq in ("\x1b[H", "\x1b[1~"):  # home
        _cursor = 0
        _render_locked()
    elif seq in ("\x1b[F", "\x1b[4~"):  # end
        _cursor = len(_buffer)
        _render_locked()
    elif seq == "\x1b[3~":  # delete
        if _cursor < len(_buffer):
            _buffer = _buffer[:_cursor] + _buffer[_cursor + 1 :]
            _render_locked()
    # Unknown ESC sequence: silently swallow.


def _reader_loop() -> None:
    """Read stdin byte-by-byte, assemble UTF-8 codepoints incrementally,
    and dispatch each char (or ESC sequence) to the buffer mutators.

    UTF-8 handling is critical: `os.read(fd, 1)` returns one byte, but a
    single Portuguese character like 'ç' is two bytes (0xC3 0xA7) — naive
    `.decode('utf-8', errors='ignore')` per byte would silently drop both
    bytes. An IncrementalDecoder accumulates bytes until a complete
    codepoint emerges.
    """
    import select

    fd = sys.stdin.fileno()
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    while not _stop_flag.is_set():
        try:
            ready, _, _ = select.select([fd], [], [], 0.1)
        except (OSError, ValueError):
            return
        if not ready:
            continue

        try:
            raw = os.read(fd, 1)
        except (OSError, ValueError):
            return
        if not raw:
            continue

        try:
            ch = decoder.decode(raw)
        except UnicodeDecodeError:
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            continue
        if not ch:
            # Mid-sequence — wait for more bytes.
            continue

        if ch == "\x1b":
            # CSI/escape sequence — pull up to 5 more bytes non-blocking.
            seq = ch
            for _ in range(5):
                try:
                    r, _, _ = select.select([fd], [], [], 0.02)
                except (OSError, ValueError):
                    break
                if not r:
                    break
                try:
                    nxt_raw = os.read(fd, 1)
                except (OSError, ValueError):
                    break
                if not nxt_raw:
                    break
                try:
                    nxt = decoder.decode(nxt_raw)
                except UnicodeDecodeError:
                    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                    break
                if not nxt:
                    continue
                seq += nxt
                # CSI final byte ends the sequence
                if nxt.isalpha() or nxt == "~":
                    break
            with _lock:
                _process_escape(seq)
            continue

        with _lock:
            _process_char(ch)


def _install_scroll_region() -> None:
    """Reserve the bottom two rows (input + pad) so the agent's print()
    flow above doesn't scroll the input row off-screen. Called from start()
    even when the thinking indicator hasn't installed its own region yet
    (chat-only replies never trigger indicator setup)."""
    global _scroll_region_set
    try:
        rows, _ = _term_size()
        # Push two blank lines and step back up (relative moves keep the
        # cursor INSIDE the region even when it started on the last row —
        # see `ThinkingIndicator._setup_scroll`), then set the scroll
        # region (1..rows-2) around a DECSC/DECRC pair, since DECSTBM
        # homes the cursor.
        sys.stdout.write(
            "\n\n" + "\033[2A" + "\0337" + f"\033[1;{rows - 2}r" + "\0338"
        )
        sys.stdout.flush()
        _scroll_region_set = True
    except (OSError, ValueError):
        pass


def _reset_scroll_region() -> None:
    global _scroll_region_set
    if not _scroll_region_set:
        return
    try:
        sys.stdout.write("\033[r")
        sys.stdout.flush()
    except (OSError, ValueError):
        pass
    _scroll_region_set = False


def start() -> bool:
    """Switch the tty into raw mode and spin up the reader thread.

    Returns True if concurrent input is now active, False otherwise
    (Windows, not a tty, disabled by env var, termios call failed).

    Buffer/cursor state is NOT reset here — they persist across
    paused()/resume() so mid-typed text survives an approval prompt.
    Initial values are set at module load.
    """
    global _active, _thread, _old_termios

    if _active:
        return True
    if not _supported():
        return False

    try:
        import termios
    except ImportError:
        return False

    fd = sys.stdin.fileno()
    try:
        old = termios.tcgetattr(fd)
    except (termios.error, OSError):
        return False

    # Disable canonical (line) mode, echo, and signal generation. We get
    # raw bytes and decide ourselves what Ctrl+C / Ctrl+Z mean.
    new = termios.tcgetattr(fd)
    new[3] &= ~(termios.ICANON | termios.ECHO | termios.ISIG)
    new[6][termios.VMIN] = 0
    new[6][termios.VTIME] = 0
    try:
        # TCSANOW: don't wait for output buffer to drain. On slow tty
        # links (ssh over poor connection) TCSADRAIN can block for
        # seconds during the apply, freezing start().
        termios.tcsetattr(fd, termios.TCSANOW, new)
    except (termios.error, OSError):
        return False

    _old_termios = old
    _stop_flag.clear()

    # Install the scroll region BEFORE marking active and spawning the
    # reader, so any output that races the spawn doesn't trample row N-1.
    _install_scroll_region()

    new_thread = threading.Thread(
        target=_reader_loop, name="alpha-concurrent-input", daemon=True
    )
    try:
        new_thread.start()
    except RuntimeError:
        # Thread creation failed — roll back termios and bail.
        try:
            termios.tcsetattr(fd, termios.TCSANOW, _old_termios)
        except (termios.error, OSError):
            pass
        _old_termios = None
        _reset_scroll_region()
        return False

    _thread = new_thread
    _active = True

    with _lock:
        _render_locked()
    return True


def stop() -> None:
    """Restore tty and stop the reader thread. Safe to call when inactive."""
    global _active, _thread, _old_termios

    if not _active:
        return

    _stop_flag.set()
    _active = False  # signal to thinking._draw and is_active() callers

    # Join the reader thread before restoring termios. Without this, the
    # daemon can still be inside os.read(fd, 1) and steal the first byte
    # the cooked-mode `input()` (approval prompt) is about to read.
    # 0.2s = 2× the select() timeout; ample margin without blocking the
    # REPL on a stuck thread.
    if _thread is not None:
        _thread.join(timeout=0.2)
        _thread = None

    if _old_termios is not None:
        try:
            import termios
            # TCSANOW for restore too — avoid blocking on output drain
            # over slow links during interrupt handling.
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, _old_termios)
        except Exception as e:
            logger.debug("concurrent input: tcsetattr restore failed: %s", e)
        _old_termios = None

    _reset_scroll_region()

    with _lock:
        _clear_input_row_locked()


@contextmanager
def paused():
    """Briefly yield the tty so cooked-mode code (approval prompt, etc.)
    can use `input()` without fighting our reader thread.

    Buffer / cursor state survives across the pause — `start()` no longer
    resets them, so any text the user was composing reappears when the
    prompt returns control.
    """
    was_active = _active
    if was_active:
        stop()
    try:
        yield
    finally:
        if was_active:
            ok = start()
            if not ok:
                logger.warning(
                    "concurrent input: failed to resume after pause "
                    "(tty closed or termios call failed) — "
                    "in-turn typing disabled for the rest of the turn"
                )

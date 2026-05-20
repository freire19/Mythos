"""Thinking indicator — animated terminal spinner with scroll regions.

Uses ANSI scroll regions (DECSTBM) to reserve the bottom terminal rows
for a persistent spinner, so streaming output flows above without erasing it.
"""

import asyncio
import logging
import os
import shutil
import sys
import time

logger = logging.getLogger(__name__)

from .core import (
    C,
    _format_duration,
    _format_tokens,
    _hint_for,
    _TODO_STATUS_GLYPH,
    c,
    is_auto_accept,
    supports_color,
)

# ─── Thinking indicator (spinner) ───

_SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
_FLOWER_FRAMES = ["✻", "✽", "✾", "✿", "❀", "✿", "✾", "✽"]

# Switches every ~8s so the user sees motion when the LLM is silent.
_THINK_VERBS = (
    "Thinking",
    "Imagining",
    "Contemplating",
    "Pondering",
    "Reasoning",
    "Reflecting",
    "Mulling",
    "Considering",
    "Deliberating",
    "Synthesizing",
    "Analyzing",
    "Cogitating",
)
_VERB_ROTATE_SECS = 8

class ThinkingIndicator:
    """Animated spinner that always stays on the bottom row of the terminal.

    Uses ANSI scroll regions (DECSTBM) to reserve the last terminal row for
    the spinner, so streaming tokens, tool calls and sub-agent events all
    flow through the scroll region above without ever erasing the spinner.

    Falls back to inline mode (\\r-rewrite on the current line) when stdout
    is not a TTY or when scroll-region setup is suppressed by env var.
    Inline mode requires callers to stop()/start() around prints to avoid
    visual conflicts; scroll-region mode does not.
    """

    # Bypass scroll region (back to old inline behavior). Useful for buggy
    # terminals or when the user wants traditional spinner display.
    _DISABLE_SCROLL_ENV = "ALPHA_NO_SCROLL_REGION"

    # Fixed reserved rows below the optional todo panel:
    #   row N-2 → spinner
    #   row N-1 → accept-edits hint (or blank when auto-accept is off)
    #   row N   → bottom padding (always blank — keeps the indicator from
    #             being glued to the terminal's bottom edge, matches the
    #             breathing room around Claude Code's status row).
    _BASE_RESERVED = 3

    # Hard cap on rendered panel rows even if the todo list is longer.
    _MAX_PANEL_ROWS = 12

    def __init__(self, label: str = "Think", style: str = "flower") -> None:
        self.label = label
        self.frames = _FLOWER_FRAMES if style == "flower" else _SPINNER_FRAMES
        self._task: asyncio.Task | None = None
        self._running = False
        self._paused = False
        self._start_time = 0.0
        self._enabled = supports_color()
        self._scroll_active = False
        self._term_rows = 0
        self._term_cols = 0
        self._streamed_chars = 0
        # Cached panel-row count for the *current* scroll region. We only
        # tear down + re-setup the scroll region when this changes, so a
        # status flip on an existing todo (pending → completed) is a cheap
        # in-place redraw with no flash.
        self._panel_capacity = 0

    # ── Scroll region lifecycle ─────────────────────────────────

    def _detect_size(self) -> tuple[int, int]:
        try:
            sz = shutil.get_terminal_size((80, 24))
            return sz.lines, sz.columns
        except Exception:
            return 24, 80

    def _scroll_supported(self) -> bool:
        if not self._enabled:
            return False
        if os.environ.get(self._DISABLE_SCROLL_ENV):
            return False
        term = os.environ.get("TERM", "").lower()
        if term in {"dumb", ""}:
            return False
        return True

    def _desired_panel_rows(self, term_rows: int) -> int:
        """How many panel rows we'd render for the current pinned todos,
        capped both by `_MAX_PANEL_ROWS` and by available terminal height
        (always leave at least 4 scrollable rows above the panel)."""
        todos = _pinned_todos or []
        if not todos:
            return 0
        # `+1` accommodates the "… +N more" overflow line.
        wanted = min(len(todos), self._MAX_PANEL_ROWS) + (
            1 if len(todos) > self._MAX_PANEL_ROWS else 0
        )
        ceiling = max(0, term_rows - self._BASE_RESERVED - 4)
        return min(wanted, ceiling)

    def _total_reserved(self) -> int:
        return self._panel_capacity + self._BASE_RESERVED

    def _setup_scroll(self) -> bool:
        """Reserve the bottom rows for the indicator panel. Idempotent."""
        if self._scroll_active:
            return True
        if not self._scroll_supported():
            return False
        rows, cols = self._detect_size()
        if rows < self._BASE_RESERVED + 2:
            return False
        self._term_rows = rows
        self._term_cols = cols
        self._panel_capacity = self._desired_panel_rows(rows)
        reserved = self._total_reserved()
        scroll_bottom = rows - reserved
        # Save the cursor (DECSC), push blank lines so content above
        # scrolls up past the soon-to-be-reserved rows, set the scroll
        # region, then restore the cursor (DECRC). Restoring keeps
        # output flowing right below the prompt instead of jumping to
        # the bottom of an otherwise-empty screen.
        out = (
            "\0337"
            + "\n" * reserved
            + f"\033[1;{scroll_bottom}r"
            + "\0338"
        )
        try:
            sys.stdout.write(out)
            sys.stdout.flush()
        except Exception:
            return False
        self._scroll_active = True
        return True

    def _teardown_scroll(self) -> None:
        if not self._scroll_active:
            return
        rows = self._term_rows or self._detect_size()[0]
        reserved = self._total_reserved()
        # Clear all reserved rows (top-down), reset scroll region, and
        # place the cursor at the bottom for whatever runs next (REPL
        # prompt, shell, etc.).
        clears = "".join(
            f"\033[{rows - i};1H\033[K"
            for i in range(reserved - 1, -1, -1)
        )
        out = clears + "\033[r" + f"\033[{rows};1H"
        try:
            sys.stdout.write(out)
            sys.stdout.flush()
        except Exception as e:
            # #DM043: stdout fechado (terminal redirecionado/encerrado) — animacao
            # silenciosamente vira no-op em vez de crashar o agent loop.
            logger.debug("thinking indicator: scroll-clear write failed: %s", e)
        self._scroll_active = False
        self._panel_capacity = 0

    def _maybe_resize(self) -> None:
        if not self._scroll_active:
            return
        rows, cols = self._detect_size()
        target_capacity = self._desired_panel_rows(rows)
        if (
            rows == self._term_rows
            and cols == self._term_cols
            and target_capacity == self._panel_capacity
        ):
            return
        self._teardown_scroll()
        self._setup_scroll()

    def refresh_layout(self) -> None:
        """Re-establish scroll region after pinned-todo count changes.
        No-op if the panel-capacity ends up unchanged (status flips, etc.),
        so most updates render in place without flashing."""
        if not self._scroll_active or not self._enabled:
            self._draw()
            return
        target = self._desired_panel_rows(self._term_rows or self._detect_size()[0])
        if target != self._panel_capacity:
            self._teardown_scroll()
            self._setup_scroll()
        self._draw()

    # ── Frame rendering ─────────────────────────────────────────

    def _select_verb(self, elapsed: float) -> str:
        """Pick the verb shown next to the spinner. The "Think" pseudo-label
        rotates through `_THINK_VERBS` so the user sees motion even when
        nothing else is changing; tool-specific labels (Reading, Bash, …)
        are shown verbatim."""
        if self.label == "Think":
            idx = int(elapsed / _VERB_ROTATE_SECS) % len(_THINK_VERBS)
            return _THINK_VERBS[idx]
        return self.label

    def _build_frame(self) -> str:
        elapsed = time.monotonic() - self._start_time
        anim_idx = int(elapsed / 0.12)
        frame = self.frames[anim_idx % len(self.frames)]
        verb = self._select_verb(elapsed)

        # Inner parens content: duration · ↓ tokens · hint
        parts: list[str] = []
        dur = _format_duration(elapsed)
        if dur:
            parts.append(dur)
        # Convert streamed chars to a token estimate only at render time;
        # accumulating raw chars avoids per-chunk rounding (a 1-char delta
        # would otherwise round up to a full token under //4).
        token_estimate = self._streamed_chars // 4
        if token_estimate > 0:
            parts.append(f"↓ {_format_tokens(token_estimate)} tokens")
        hint = _hint_for(elapsed)
        if hint:
            parts.append(hint)

        if parts:
            paren = c(C.GRAY, " (" + " · ".join(parts) + ")")
        else:
            paren = ""

        spinner_part = c(C.ORANGE + C.BOLD, frame)
        # Breathe between two close violet shades over a 2-second cycle —
        # alive but not flicker-y. `int(elapsed * 2) % 4` ticks once per
        # 0.5s, giving 1s VIOLET / 1s VIOLET_GLOW.
        verb_color = C.VIOLET_GLOW if int(elapsed * 2) % 4 >= 2 else C.VIOLET
        verb_part = c(verb_color + C.BOLD, f"{verb}…")
        return f"{spinner_part} {verb_part}{paren}"

    # (suffix-shown, plain-text-length used for the column-fit check). Picks
    # the widest variant that fits; empty suffix means just the prefix.
    _STATUS_VARIANTS = (
        ("(shift+tab to cycle)", len("▸▸ accept edits on (shift+tab to cycle)") + 2),
        ("(shift+tab)",          len("▸▸ accept edits on (shift+tab)") + 2),
        ("",                     len("▸▸ accept edits on") + 2),
    )

    def _build_status(self) -> str:
        """Second reserved row — accept-edits state mirror. Empty when the
        feature is off so the row stays visually quiet."""
        if not is_auto_accept():
            return ""
        cols = self._term_cols or self._detect_size()[1]
        prefix = (
            f"{c(C.AMBER_SOFT + C.BOLD, '▸▸')} "
            f"{c(C.AMBER_SOFT + C.BOLD, 'accept edits on')}"
        )
        for suffix, min_cols in self._STATUS_VARIANTS:
            if cols >= min_cols:
                if not suffix:
                    return prefix
                return f"{prefix} {c(C.GRAY_DARK, suffix)}"
        return ""

    def _build_panel_lines(self) -> list[str]:
        """Render the pinned-todo panel as a list of pre-colored lines, one
        per panel row. Truncates content to fit terminal width; appends a
        `… +N more` row if the list exceeds `_panel_capacity`."""
        todos = _pinned_todos or []
        if not todos or self._panel_capacity == 0:
            return []
        cols = self._term_cols or self._detect_size()[1]
        # `  ` indent + glyph + space = 4 visible chars, plus a small margin.
        max_content = max(20, cols - 6)
        # If overflow row will be needed, reserve one slot for it.
        overflow = len(todos) > self._panel_capacity
        visible_count = self._panel_capacity - 1 if overflow else self._panel_capacity
        lines: list[str] = []
        for t in todos[:visible_count]:
            if not isinstance(t, dict):
                continue
            status = str(t.get("status", "pending"))
            glyph, color = _TODO_STATUS_GLYPH.get(status, ("•", C.GRAY))
            content = str(t.get("content", ""))
            if len(content) > max_content:
                content = content[: max_content - 1] + "…"
            line_color = C.GRAY if status in ("completed", "cancelled") else C.WHITE
            lines.append(f"  {c(color, glyph)} {c(line_color, content)}")
        if overflow:
            remaining = len(todos) - visible_count
            lines.append(f"  {c(C.GRAY_DARK, f'… +{remaining} more')}")
        return lines

    def _draw(self) -> None:
        if not self._running or self._paused or not self._enabled:
            return
        frame_text = self._build_frame()

        if not self._scroll_active:
            sys.stdout.write(f"\r{frame_text}\033[K")
            sys.stdout.flush()
            return

        status_text = self._build_status()
        panel_lines = self._build_panel_lines()
        rows = self._term_rows
        reserved = self._total_reserved()
        # Layout (top→bottom): panel rows · spinner · status · blank pad.
        # Bottom row stays blank so the indicator isn't glued to the edge.
        panel_top = rows - reserved + 1
        spinner_row = rows - 2
        status_row = rows - 1

        out_parts = ["\033[s"]
        for i, line in enumerate(panel_lines):
            out_parts.append(f"\033[{panel_top + i};1H\033[K{line}")
        out_parts.append(f"\033[{spinner_row};1H\033[K{frame_text}")
        out_parts.append(f"\033[{status_row};1H\033[K{status_text}")
        out_parts.append("\033[u")
        try:
            sys.stdout.write("".join(out_parts))
            sys.stdout.flush()
        except Exception as e:
            # #DM043: stdout indisponivel mid-animacao — no-op silencioso.
            logger.debug("thinking indicator: frame write failed: %s", e)

    # ── Public API ──────────────────────────────────────────────

    def start(self, label: str | None = None) -> None:
        global _active_indicator
        if not self._enabled:
            if label:
                self.label = label
            return
        if self._running:
            if label:
                self.label = label
            # Idempotent: also clears any pause set by approval_needed,
            # so callers don't need to know whether the indicator is
            # paused or just running.
            self._paused = False
            self._draw()
            return
        if label:
            self.label = label
        self._setup_scroll()
        self._running = True
        self._paused = False
        self._start_time = time.monotonic()
        _active_indicator = self
        try:
            loop = asyncio.get_running_loop()
            self._task = loop.create_task(self._animate())
        except RuntimeError:
            self._running = False
            self._teardown_scroll()
            if _active_indicator is self:
                _active_indicator = None

    def stop(self) -> None:
        global _active_indicator
        if not self._running:
            # Even if not running, we may still hold an orphan scroll region
            # (e.g. process about to exit) — tear down defensively.
            if self._scroll_active:
                self._teardown_scroll()
            if _active_indicator is self:
                _active_indicator = None
            return
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
        if self._scroll_active:
            self._teardown_scroll()
        elif self._enabled:
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()
        if _active_indicator is self:
            _active_indicator = None

    def pause(self) -> None:
        """Suppress redraws without killing the task. Used during input().
        Clears the spinner + status rows but leaves the todo panel visible
        so the user still has context while answering an approval prompt."""
        self._paused = True
        if self._scroll_active and self._enabled:
            rows = self._term_rows
            clears = "".join(
                f"\033[{rows - i};1H\033[K"
                for i in range(self._BASE_RESERVED - 1, -1, -1)
            )
            try:
                sys.stdout.write(f"\033[s{clears}\033[u")
                sys.stdout.flush()
            except Exception as e:
                # #DM043: pause animacao quando stdout indisponivel — no-op.
                logger.debug("thinking indicator: pause-clear failed: %s", e)

    def resume(self) -> None:
        self._paused = False
        self._draw()

    def update_label(self, label: str) -> None:
        self.label = label
        # Force immediate frame so label change is visible without waiting
        # for the next 0.12s tick.
        self._draw()

    def add_streamed_text(self, text: str) -> None:
        """Track chars streamed during the current turn — surfaced in the
        spinner's parens as `↓ <count> tokens` (estimated at render time)."""
        if text:
            self._streamed_chars += len(text)

    async def _animate(self) -> None:
        try:
            while self._running:
                self._maybe_resize()
                self._draw()
                await asyncio.sleep(0.12)
        except asyncio.CancelledError:
            pass


_active_indicator: "ThinkingIndicator | None" = None

# Most recent todo list pinned above the spinner. Updated by
# `set_pinned_todos` (typically from print_tool_result on todo_write).
# Survives across turns until cleared so the user keeps the checklist
# context between prompts.
_pinned_todos: "list[dict] | None" = None


def get_active_indicator() -> "ThinkingIndicator | None":
    """Return the currently active indicator, if any."""
    return _active_indicator


def set_pinned_todos(todos: "list[dict] | None") -> None:
    """Pin a todo list above the spinner. Pass an empty list or None to
    clear. Triggers an immediate redraw of the active indicator (if any)
    and resizes the scroll region when the panel row count changes."""
    global _pinned_todos
    _pinned_todos = list(todos) if todos else None
    ind = _active_indicator
    if ind is not None:
        ind.refresh_layout()


def get_pinned_todos() -> "list[dict] | None":
    return _pinned_todos


def cleanup_indicator() -> None:
    """Reset scroll region on interpreter exit. Registered as an atexit
    hook from ``install_lifecycle_hooks`` so terminal isn't left stuck
    in a clipped state if the agent crashes before stop() runs."""
    ind = _active_indicator
    if ind is None:
        return
    try:
        ind.stop()
    except Exception as e:
        # #DM043: atexit path — terminal pode estar half-closed. Debug log
        # via logger (nao print) porque atexit pode ter stderr fechado tambem.
        logger.debug("indicator cleanup failed: %s", e)
